// SPDX-License-Identifier: Apache-2.0
//! Finite CPU admission RPC. No retries or detached HTTP sends. Blocking I/O
//! stays off the async executor; at most eight jobs survive caller cancellation.
use serde_json::{json, Value};
use std::{
    io::{Read, Write},
    net::{SocketAddr, TcpStream},
    sync::{
        atomic::{AtomicUsize, Ordering},
        Arc,
    },
    time::{Duration, Instant},
};

#[derive(Debug)]
pub struct AdmissionClient {
    address: SocketAddr,
    token: String,
    active: Arc<AtomicUsize>,
}

struct Slot(Arc<AtomicUsize>);
impl Drop for Slot {
    fn drop(&mut self) {
        self.0.fetch_sub(1, Ordering::SeqCst);
    }
}

impl AdmissionClient {
    pub fn new(address: SocketAddr, token: String) -> Result<Self, &'static str> {
        if address.ip() != std::net::Ipv4Addr::LOCALHOST
            || address.port() == 0
            || token.len() != 64
            || !token
                .bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
        {
            return Err("invalid_admission_endpoint");
        }
        Ok(Self {
            address,
            token,
            active: Arc::new(AtomicUsize::new(0)),
        })
    }

    pub async fn reserve(
        &self,
        request_id: String,
        attempt_id: String,
        at_ns: u64,
    ) -> Result<(), &'static str> {
        let mut count = self.active.load(Ordering::SeqCst);
        loop {
            if count >= 8 {
                return Err("admission_busy");
            }
            match self.active.compare_exchange_weak(
                count,
                count + 1,
                Ordering::SeqCst,
                Ordering::SeqCst,
            ) {
                Ok(_) => break,
                Err(observed) => count = observed,
            }
        }
        let slot = Slot(self.active.clone());
        let address = self.address;
        let message = json!({"schema":"admission-cpu-v1", "token": self.token,
            "request_id":request_id, "attempt_id":attempt_id,
            "clock_id":"fixture-router-session", "at_ns":at_ns});
        // Cancellation does not kill this finite job. A commit without receipt
        // remains charged. Never retry a reservation or dispatch from this job.
        tokio::task::spawn_blocking(move || {
            let _slot = slot;
            exchange(address, message)
        })
        .await
        .map_err(|_| "admission_task_failed")?
    }
}

fn exchange(address: SocketAddr, message: Value) -> Result<(), &'static str> {
    let deadline = Instant::now() + Duration::from_secs(2);
    let mut connection = TcpStream::connect_timeout(&address, Duration::from_millis(200))
        .map_err(|_| "admission_connect")?;
    let payload = serde_json::to_vec(&message).map_err(|_| "admission_encode")?;
    if payload.len() > 2048 {
        return Err("admission_frame_limit");
    }
    let mut bytes = (payload.len() as u32).to_be_bytes().to_vec();
    bytes.extend_from_slice(&payload);
    while !bytes.is_empty() {
        connection
            .set_write_timeout(Some(remaining(deadline)?))
            .map_err(|_| "admission_io")?;
        let size = connection.write(&bytes).map_err(|_| "admission_io")?;
        if size == 0 {
            return Err("admission_io");
        }
        bytes.drain(..size);
    }
    let mut size = [0; 4];
    read(&mut connection, &mut size, deadline)?;
    let size = u32::from_be_bytes(size) as usize;
    if size == 0 || size > 2048 {
        return Err("admission_frame_limit");
    }
    let mut payload = vec![0; size];
    read(&mut connection, &mut payload, deadline)?;
    let ack: Value = serde_json::from_slice(&payload).map_err(|_| "admission_decode")?;
    if ack["schema"] != "admission-cpu-v1"
        || ack["result"] != "applied"
        || ack["request_id"] != message["request_id"]
        || ack["attempt_id"] != message["attempt_id"]
        || ack["clock_id"] != message["clock_id"]
        || ack["at_ns"] != message["at_ns"]
    {
        return Err("admission_no_permit");
    }
    Ok(())
}

fn remaining(deadline: Instant) -> Result<Duration, &'static str> {
    deadline
        .checked_duration_since(Instant::now())
        .filter(|d| !d.is_zero())
        .ok_or("admission_timeout")
}

fn read(
    connection: &mut TcpStream,
    mut buffer: &mut [u8],
    deadline: Instant,
) -> Result<(), &'static str> {
    while !buffer.is_empty() {
        connection
            .set_read_timeout(Some(remaining(deadline)?))
            .map_err(|_| "admission_io")?;
        let size = connection.read(buffer).map_err(|_| "admission_io")?;
        if size == 0 {
            return Err("admission_missing_receipt");
        }
        buffer = &mut buffer[size..];
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{net::TcpListener, thread};

    fn peer(mode: &'static str) -> (SocketAddr, thread::JoinHandle<()>) {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        listener.set_nonblocking(true).unwrap();
        let address = listener.local_addr().unwrap();
        let task = thread::spawn(move || {
            let stop = Instant::now() + Duration::from_secs(4);
            let mut connection = loop {
                if let Ok((connection, _)) = listener.accept() {
                    break connection;
                }
                assert!(Instant::now() < stop, "test peer accept deadline");
                thread::sleep(Duration::from_millis(5));
            };
            let mut size = [0; 4];
            read(&mut connection, &mut size, stop).unwrap();
            let mut buffer = vec![0; u32::from_be_bytes(size) as usize];
            assert!(buffer.len() <= 2048);
            read(&mut connection, &mut buffer, stop).unwrap();
            let mut ack: Value = serde_json::from_slice(&buffer).unwrap();
            if mode == "lost" {
                return;
            }
            if mode == "delayed" {
                thread::sleep(Duration::from_millis(150));
            }
            ack.as_object_mut().unwrap().remove("token");
            ack["result"] = json!(if mode == "denied" {
                "rejected"
            } else {
                "applied"
            });
            if mode == "wrong" {
                ack["attempt_id"] = json!("different");
            }
            let buffer = serde_json::to_vec(&ack).unwrap();
            connection
                .set_write_timeout(Some(Duration::from_secs(1)))
                .unwrap();
            let _ = connection.write_all(&(buffer.len() as u32).to_be_bytes());
            let _ = connection.write_all(&buffer);
        });
        (address, task)
    }

    #[tokio::test]
    async fn admission_requires_exact_positive_receipt() {
        for mode in ["ok", "wrong", "denied", "lost"] {
            let (address, peer) = peer(mode);
            let client = AdmissionClient::new(address, "ab".repeat(32)).unwrap();
            assert_eq!(
                client
                    .reserve("request".into(), "attempt".into(), 7)
                    .await
                    .is_ok(),
                mode == "ok"
            );
            peer.join().unwrap();
            assert_eq!(client.active.load(Ordering::SeqCst), 0);
        }
    }

    #[tokio::test]
    async fn cancelled_wait_does_not_dispatch_or_leak_job_slot() {
        let (address, peer) = peer("delayed");
        let client = AdmissionClient::new(address, "ab".repeat(32)).unwrap();
        assert!(tokio::time::timeout(
            Duration::from_millis(50),
            client.reserve("request".into(), "attempt".into(), 7)
        )
        .await
        .is_err());
        // The owner may already have committed. No permit was delivered, and
        // only the finite RPC job survives cancellation (it cannot send HTTP).
        peer.join().unwrap();
        tokio::time::timeout(Duration::from_secs(1), async {
            while client.active.load(Ordering::SeqCst) != 0 {
                tokio::time::sleep(Duration::from_millis(5)).await;
            }
        })
        .await
        .unwrap();
        assert_eq!(client.active.load(Ordering::SeqCst), 0);
    }

    #[tokio::test]
    async fn full_rpc_capacity_rejects_without_connecting() {
        let client = AdmissionClient::new("127.0.0.1:1".parse().unwrap(), "ab".repeat(32)).unwrap();
        client.active.store(8, Ordering::SeqCst);
        assert_eq!(
            client.reserve("request".into(), "attempt".into(), 7).await,
            Err("admission_busy")
        );
        assert_eq!(client.active.load(Ordering::SeqCst), 8);
    }

    #[test]
    fn remote_endpoint_and_weak_capability_rejected() {
        assert!(AdmissionClient::new("10.0.0.1:80".parse().unwrap(), "ab".repeat(32)).is_err());
        assert!(AdmissionClient::new("127.0.0.1:80".parse().unwrap(), "weak".into()).is_err());
    }
}
