// SPDX-License-Identifier: Apache-2.0
//! Finite CPU transport-observation sink. No I/O in Lease callbacks, no backend
//! termination inference, no production remote ledger service or dynamic GC.
use super::{
    lifecycle_bridge::{LifecycleEvent, LifecycleTrace, TracedLease},
    prefix_extension::{CloseEvidence, Lease},
};
use serde::Deserialize;
use std::{
    io::{Read, Write},
    net::{Ipv4Addr, SocketAddr, TcpStream},
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc, Mutex,
    },
    thread::{self, JoinHandle},
    time::{Duration, Instant},
};

const SCHEMA: &str = "transport-observer-cpu-v1";

#[derive(Debug)]
struct Slot {
    trace: Arc<Mutex<LifecycleTrace>>,
    acknowledged: usize,
}

#[derive(Debug)]
pub struct Outbox {
    slots: Mutex<Vec<Slot>>,
    capacity: usize,
    failed: AtomicBool,
    controller_active: AtomicBool,
}

impl Outbox {
    pub fn new(capacity: usize) -> Result<Self, &'static str> {
        if !(1..=128).contains(&capacity) {
            return Err("outbox_capacity");
        }
        Ok(Self {
            slots: Mutex::new(Vec::with_capacity(capacity)),
            capacity,
            failed: AtomicBool::new(false),
            controller_active: AtomicBool::new(false),
        })
    }

    pub fn admissible(&self) -> bool {
        !self.failed.load(Ordering::Acquire)
    }

    pub fn register(&self, trace: Arc<Mutex<LifecycleTrace>>) -> Result<(), &'static str> {
        if !self.admissible() {
            return Err("outbox_disconnected");
        }
        let mut slots = self.slots.lock().map_err(|_| "outbox_poisoned")?;
        if slots.len() == self.capacity {
            return Err("outbox_full");
        }
        let id = trace
            .lock()
            .map_err(|_| "trace_poisoned")?
            .identity()
            .to_owned();
        for slot in slots.iter() {
            if slot.trace.lock().map_err(|_| "trace_poisoned")?.identity() == id {
                return Err("outbox_attempt_reused");
            }
        }
        slots.push(Slot {
            trace,
            acknowledged: 0,
        });
        Ok(())
    }

    fn next(&self) -> Result<Option<(usize, LifecycleEvent)>, &'static str> {
        let slots = self.slots.lock().map_err(|_| "outbox_poisoned")?;
        for (index, slot) in slots.iter().enumerate() {
            let trace = slot.trace.lock().map_err(|_| "trace_poisoned")?;
            if let Some(event) = trace.pending(slot.acknowledged)?.into_iter().next() {
                return Ok(Some((index, event)));
            }
        }
        Ok(None)
    }

    fn acknowledge(&self, index: usize, sequence: u64) -> Result<(), &'static str> {
        let mut slots = self.slots.lock().map_err(|_| "outbox_poisoned")?;
        let slot = slots.get_mut(index).ok_or("outbox_slot")?;
        if sequence != slot.acknowledged as u64 + 1 {
            return Err("outbox_ack_gap");
        }
        slot.acknowledged += 1;
        Ok(())
    }

    pub fn drained(&self) -> bool {
        let Ok(slots) = self.slots.lock() else {
            return false;
        };
        slots.iter().all(|slot| {
            slot.trace
                .lock()
                .is_ok_and(|trace| trace.delivered(slot.acknowledged))
        })
    }
}

#[derive(Debug)]
pub struct OnlineLease {
    pub inner: TracedLease,
    pub outbox: Arc<Outbox>,
}

impl Lease for OnlineLease {
    fn before_send(&self) -> Result<(), &'static str> {
        if !self.outbox.admissible() {
            return Err("outbox_disconnected");
        }
        self.inner.before_send()
    }
    fn sent(&self) {
        self.inner.sent();
    }
    fn headers(&self, status: u16) {
        self.inner.headers(status);
    }
    fn closed(&self, sent: bool, evidence: CloseEvidence) {
        self.inner.closed(sent, evidence);
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Receipt {
    schema: String,
    result: String,
    event_id: String,
    attempt_id: String,
    clock_id: String,
    sequence: u64,
}

fn deliver(address: SocketAddr, token: &str, event: &LifecycleEvent) -> Result<(), &'static str> {
    let payload = serde_json::to_vec(&serde_json::json!({
        "schema": SCHEMA, "token": token, "event": event,
    }))
    .map_err(|_| "encode")?;
    if payload.len() > 8192 {
        return Err("frame_budget");
    }
    let deadline = Instant::now() + Duration::from_millis(1000);
    let mut stream =
        TcpStream::connect_timeout(&address, Duration::from_millis(200)).map_err(|_| "connect")?;
    let mut frame = (payload.len() as u32).to_be_bytes().to_vec();
    frame.extend_from_slice(&payload);
    let mut written = 0;
    while written < frame.len() {
        let remaining = deadline
            .checked_duration_since(Instant::now())
            .ok_or("deadline")?;
        stream
            .set_write_timeout(Some(remaining))
            .map_err(|_| "timeout")?;
        let count = stream.write(&frame[written..]).map_err(|_| "write")?;
        if count == 0 {
            return Err("write_eof");
        }
        written += count;
    }
    // Absolute receipt deadline, including partial header/payload arrivals.
    fn read_until(
        stream: &mut TcpStream,
        bytes: &mut [u8],
        deadline: Instant,
    ) -> Result<(), &'static str> {
        let mut offset = 0;
        while offset < bytes.len() {
            let remaining = deadline
                .checked_duration_since(Instant::now())
                .ok_or("deadline")?;
            stream
                .set_read_timeout(Some(remaining))
                .map_err(|_| "timeout")?;
            let count = stream.read(&mut bytes[offset..]).map_err(|_| "read")?;
            if count == 0 {
                return Err("eof");
            }
            offset += count;
        }
        Ok(())
    }
    let mut header = [0; 4];
    read_until(&mut stream, &mut header, deadline)?;
    let length = u32::from_be_bytes(header) as usize;
    if !(1..=2048).contains(&length) {
        return Err("receipt_budget");
    }
    let mut response = vec![0; length];
    read_until(&mut stream, &mut response, deadline)?;
    let receipt: Receipt = serde_json::from_slice(&response).map_err(|_| "receipt")?;
    if receipt.schema != SCHEMA
        || !matches!(receipt.result.as_str(), "applied" | "duplicate")
        || receipt.event_id != event.event_id
        || receipt.attempt_id != event.attempt_id
        || receipt.clock_id != event.clock_id
        || receipt.sequence != event.sequence
    {
        return Err("receipt_binding");
    }
    Ok(())
}

pub struct Controller {
    stop: Arc<AtomicBool>,
    join: Option<JoinHandle<()>>,
    outbox: Arc<Outbox>,
}

impl Controller {
    pub fn start(
        outbox: Arc<Outbox>,
        address: SocketAddr,
        token: String,
    ) -> Result<Self, &'static str> {
        if address.ip() != Ipv4Addr::LOCALHOST
            || address.port() == 0
            || token.len() != 64
            || !token
                .bytes()
                .all(|b| b.is_ascii_hexdigit() && !b.is_ascii_uppercase())
        {
            return Err("loopback_capability_required");
        }
        let stop = Arc::new(AtomicBool::new(false));
        if outbox
            .controller_active
            .compare_exchange(false, true, Ordering::AcqRel, Ordering::Acquire)
            .is_err()
        {
            return Err("controller_already_active");
        }
        let stopping = stop.clone();
        let pending = outbox.clone();
        let join = thread::Builder::new()
            .name("cpu-receipt-delivery".into())
            .spawn(move || {
                while !stopping.load(Ordering::Acquire) {
                    match pending.next() {
                        Ok(Some((index, event))) => {
                            if deliver(address, &token, &event).is_ok() {
                                if pending.acknowledge(index, event.sequence).is_err() {
                                    pending.failed.store(true, Ordering::Release);
                                    break;
                                }
                                continue;
                            }
                            // Reconnect retries may drain old records but never
                            // silently re-enable admission after a channel fault.
                            pending.failed.store(true, Ordering::Release);
                        }
                        Ok(None) => {}
                        Err(_) => {
                            pending.failed.store(true, Ordering::Release);
                            break;
                        }
                    }
                    thread::sleep(Duration::from_millis(10));
                }
            })
            .map_err(|_| {
                outbox.controller_active.store(false, Ordering::Release);
                "controller_spawn"
            })?;
        Ok(Self {
            stop,
            join: Some(join),
            outbox,
        })
    }

    pub fn drain(&self, timeout: Duration) -> Result<(), &'static str> {
        let deadline = Instant::now() + timeout;
        while !self.outbox.drained() {
            if Instant::now() >= deadline {
                return Err("unacknowledged_observations");
            }
            thread::sleep(Duration::from_millis(10));
        }
        Ok(())
    }
}

impl Drop for Controller {
    fn drop(&mut self) {
        self.outbox.failed.store(true, Ordering::Release);
        self.stop.store(true, Ordering::Release);
        if let Some(join) = self.join.take() {
            let _ = join.join();
        }
        self.outbox
            .controller_active
            .store(false, Ordering::Release);
    }
}

#[cfg(test)]
mod tests {
    use super::super::lifecycle_bridge::RouterClock;
    use super::*;
    use std::net::TcpListener;

    #[derive(Debug)]
    struct Inner;
    impl Lease for Inner {
        fn before_send(&self) -> Result<(), &'static str> {
            Ok(())
        }
        fn sent(&self) {}
        fn headers(&self, _: u16) {}
        fn closed(&self, _: bool, _: CloseEvidence) {}
    }

    fn lease(outbox: &Arc<Outbox>, id: &str) -> OnlineLease {
        let trace = Arc::new(Mutex::new(
            LifecycleTrace::new(
                format!("request-{id}"),
                id.into(),
                "worker".into(),
                "instance".into(),
                Arc::new(RouterClock::new("clock".into()).unwrap()),
            )
            .unwrap(),
        ));
        outbox.register(trace.clone()).unwrap();
        OnlineLease {
            inner: TracedLease {
                inner: Box::new(Inner),
                trace,
            },
            outbox: outbox.clone(),
        }
    }

    fn request(stream: &mut TcpStream) -> serde_json::Value {
        stream
            .set_read_timeout(Some(Duration::from_secs(2)))
            .unwrap();
        let mut header = [0; 4];
        stream.read_exact(&mut header).unwrap();
        let length = u32::from_be_bytes(header) as usize;
        assert!(length <= 8192);
        let mut payload = vec![0; length];
        stream.read_exact(&mut payload).unwrap();
        serde_json::from_slice(&payload).unwrap()
    }

    fn reply(stream: &mut TcpStream, request: &serde_json::Value) {
        let event = &request["event"];
        let payload = serde_json::to_vec(&serde_json::json!({
            "schema": SCHEMA, "result": "duplicate", "event_id": event["event_id"],
            "attempt_id": event["attempt_id"], "clock_id": event["clock_id"], "sequence": event["sequence"],
        })).unwrap();
        stream
            .write_all(&(payload.len() as u32).to_be_bytes())
            .unwrap();
        stream.write_all(&payload).unwrap();
    }

    #[test]
    fn disconnect_replays_exact_payload_and_keeps_admission_paused() {
        let outbox = Arc::new(Outbox::new(1).unwrap());
        let lease = lease(&outbox, "a");
        lease.before_send().unwrap();
        lease.sent();
        lease.closed(true, CloseEvidence::TransportEof);
        let listener = TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).unwrap();
        let address = listener.local_addr().unwrap();
        let peer = thread::spawn(move || {
            let (mut first, _) = listener.accept().unwrap();
            let original = request(&mut first);
            drop(first); // Simulates a lost ACK after the receiver committed.
            let (mut retry, _) = listener.accept().unwrap();
            let repeated = request(&mut retry);
            assert_eq!(original, repeated);
            reply(&mut retry, &repeated);
            let (mut second, _) = listener.accept().unwrap();
            let close = request(&mut second);
            assert_eq!(close["event"]["kind"], "outcome_unknown");
            reply(&mut second, &close);
        });
        let controller = Controller::start(outbox.clone(), address, "ab".repeat(32)).unwrap();
        controller.drain(Duration::from_secs(4)).unwrap();
        peer.join().unwrap();
        assert!(!outbox.admissible());
        assert_eq!(lease.before_send(), Err("outbox_disconnected"));
        assert!(outbox.next().unwrap().is_none());
    }

    #[test]
    fn no_receiver_retains_both_callbacks_and_full_slots_reject_new_work() {
        let outbox = Arc::new(Outbox::new(1).unwrap());
        let lease = lease(&outbox, "a");
        assert!(outbox.register(lease.inner.trace.clone()).is_err());
        lease.sent();
        lease.closed(true, CloseEvidence::TransportDropped);
        lease.closed(true, CloseEvidence::TransportDropped);
        assert_eq!(
            lease.inner.trace.lock().unwrap().pending(0).unwrap().len(),
            2
        );
        assert!(!outbox.drained());
        assert_eq!(outbox.next().unwrap().unwrap().1.sequence, 1);
    }

    #[test]
    fn invalid_ack_never_advances_cursor() {
        for oversized in [false, true] {
            let outbox = Arc::new(Outbox::new(1).unwrap());
            let lease = lease(&outbox, "a");
            lease.closed(false, CloseEvidence::NotSent);
            let event = outbox.next().unwrap().unwrap().1;
            let listener = TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).unwrap();
            let address = listener.local_addr().unwrap();
            let peer = thread::spawn(move || {
                let (mut stream, _) = listener.accept().unwrap();
                let mut value = request(&mut stream);
                if oversized {
                    stream.write_all(&8193u32.to_be_bytes()).unwrap();
                } else {
                    value["event"]["attempt_id"] = "wrong-attempt".into();
                    reply(&mut stream, &value);
                }
            });
            assert!(deliver(address, &"ab".repeat(32), &event).is_err());
            peer.join().unwrap();
            assert_eq!(outbox.next().unwrap().unwrap().1.sequence, 1);
            assert!(!outbox.drained());
        }
    }

    #[test]
    fn capability_target_and_single_controller_are_enforced() {
        let outbox = Arc::new(Outbox::new(2).unwrap());
        assert!(Controller::start(
            outbox.clone(),
            "192.0.2.1:12".parse().unwrap(),
            "ab".repeat(32)
        )
        .is_err());
        assert!(Controller::start(
            outbox.clone(),
            "127.0.0.1:12".parse().unwrap(),
            "short".into()
        )
        .is_err());
        let owner = Controller::start(
            outbox.clone(),
            "127.0.0.1:12".parse().unwrap(),
            "ab".repeat(32),
        )
        .unwrap();
        assert!(Controller::start(
            outbox.clone(),
            "127.0.0.1:12".parse().unwrap(),
            "ab".repeat(32)
        )
        .is_err());
        drop(owner);
        let _replacement =
            Controller::start(outbox, "127.0.0.1:12".parse().unwrap(), "ab".repeat(32)).unwrap();
    }

    #[test]
    fn duplicate_identity_rejected_even_with_free_capacity() {
        let outbox = Arc::new(Outbox::new(2).unwrap());
        let lease = lease(&outbox, "a");
        assert_eq!(
            outbox.register(lease.inner.trace.clone()),
            Err("outbox_attempt_reused")
        );
    }

    #[test]
    fn stopped_controller_cannot_leave_dispatch_enabled() {
        let outbox = Arc::new(Outbox::new(1).unwrap());
        let owner = Controller::start(
            outbox.clone(),
            "127.0.0.1:12".parse().unwrap(),
            "ab".repeat(32),
        )
        .unwrap();
        drop(owner);
        assert!(!outbox.admissible());
    }

    #[test]
    fn peer_without_receipt_hits_deadline_and_preserves_pending_record() {
        let outbox = Arc::new(Outbox::new(1).unwrap());
        let lease = lease(&outbox, "a");
        lease.closed(false, CloseEvidence::NotSent);
        let event = outbox.next().unwrap().unwrap().1;
        let listener = TcpListener::bind((Ipv4Addr::LOCALHOST, 0)).unwrap();
        let address = listener.local_addr().unwrap();
        let peer = thread::spawn(move || {
            let (mut stream, _) = listener.accept().unwrap();
            request(&mut stream);
            thread::sleep(Duration::from_millis(1250));
        });
        let start = Instant::now();
        assert!(deliver(address, &"ab".repeat(32), &event).is_err());
        assert!(start.elapsed() < Duration::from_secs(3));
        peer.join().unwrap();
        assert_eq!(outbox.next().unwrap().unwrap().1.sequence, 1);
    }
}
