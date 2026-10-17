// SPDX-License-Identifier: Apache-2.0
use super::*;
use crate::directory::Binding;
use std::{
    collections::VecDeque,
    io::{Read, Write},
    net::{TcpListener, TcpStream},
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc,
    },
    thread::{self, JoinHandle},
    time::Instant,
};

const TOKEN: &str = "0123456789abcdef0123456789abcdef";

fn body() -> serde_json::Value {
    serde_json::json!({
        "schema_version": 1,
        "binding": {"worker_id": "replica-a", "instance_epoch": "core-a", "observer_epoch": "observer-a"},
        "frontend_pid": 101, "core_pid": 102, "core_boundary": 0,
        "frontend_boundary": 0, "family_count": 0, "reserved_children": 0,
        "acknowledged": 0, "retired_through": 0,
        "ownership_mode": "observe_only", "disabled": false
    })
}

struct Server {
    origin: String,
    stop: Arc<AtomicBool>,
    handle: Option<JoinHandle<()>>,
}

fn request(stream: &mut TcpStream) {
    stream
        .set_read_timeout(Some(Duration::from_secs(2)))
        .unwrap();
    stream
        .set_write_timeout(Some(Duration::from_secs(2)))
        .unwrap();
    let mut bytes = Vec::new();
    while !bytes.ends_with(b"\r\n\r\n") {
        let mut next = [0; 1];
        stream.read_exact(&mut next).unwrap();
        bytes.push(next[0]);
        assert!(bytes.len() < 8192);
    }
    let headers = String::from_utf8(bytes).unwrap();
    assert!(headers.starts_with("GET /prefix-routing/identity HTTP/1.1\r\n"));
    assert!(headers
        .to_ascii_lowercase()
        .contains(&format!("x-prefix-routing-control: {TOKEN}")));
}

impl Server {
    fn new(replies: Vec<(u16, String)>) -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        listener.set_nonblocking(true).unwrap();
        let origin = format!("http://{}", listener.local_addr().unwrap());
        let stop = Arc::new(AtomicBool::new(false));
        let worker_stop = stop.clone();
        let handle = thread::spawn(move || {
            let mut replies = VecDeque::from(replies);
            let deadline = Instant::now() + Duration::from_secs(10);
            while !worker_stop.load(Ordering::Acquire) && !replies.is_empty() {
                assert!(Instant::now() < deadline);
                let (mut stream, _) = match listener.accept() {
                    Ok(value) => value,
                    Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                        thread::sleep(Duration::from_millis(2));
                        continue;
                    }
                    Err(error) => panic!("fixture accept: {error}"),
                };
                request(&mut stream);
                let (status, body) = replies.pop_front().unwrap();
                write!(stream, "HTTP/1.1 {status} Fixture\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}", body.len()).unwrap();
            }
        });
        Self {
            origin,
            stop,
            handle: Some(handle),
        }
    }
}

impl Drop for Server {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::Release);
        let result = self.handle.take().unwrap().join();
        if !thread::panicking() {
            result.unwrap();
        }
    }
}

fn spec(origin: &str) -> PartitionSpec {
    PartitionSpec {
        binding: Binding {
            worker: Arc::from(origin),
            instance: 1,
            publisher: Arc::from("local-connection"),
            identity: Arc::from("a".repeat(64)),
            block_size: 128,
        },
        instance_epoch: Arc::from("core-a"),
    }
}

#[test]
fn actual_identity_reads_monotonic_source_without_receipt_ack_or_token_logging() {
    let initial = body();
    let mut current = body();
    current["core_boundary"] = serde_json::json!(4);
    current["frontend_boundary"] = serde_json::json!(1);
    let server = Server::new(vec![(200, initial.to_string()), (200, current.to_string())]);
    let mut control =
        HustSourceControl::connect_cold(&server.origin, TOKEN.into(), "replica-a", 101).unwrap();
    assert!(!format!("{control:?}").contains(TOKEN));
    control.verify(&spec(&server.origin)).unwrap();
    assert_eq!(control.identity().core_boundary, 4);
}

#[test]
fn actual_changed_disabled_malformed_or_oversize_identity_is_rejected() {
    let mut values = Vec::new();
    for field in ["core_pid", "frontend_pid", "disabled", "schema_version"] {
        let mut changed = body();
        changed[field] = match field {
            "disabled" => serde_json::json!(true),
            "schema_version" => serde_json::json!(true),
            _ => serde_json::json!(999),
        };
        values.push((200, changed.to_string()));
    }
    let mut changed = body();
    changed["binding"]["instance_epoch"] = serde_json::json!("core-b");
    values.push((200, changed.to_string()));
    values.push((200, " ".repeat(MAX_IDENTITY_BYTES as usize + 1)));
    values.push((302, body().to_string()));
    for response in values {
        let server = Server::new(vec![(200, body().to_string()), response]);
        let mut control =
            HustSourceControl::connect_cold(&server.origin, TOKEN.into(), "replica-a", 101)
                .unwrap();
        assert_eq!(
            control.verify(&spec(&server.origin)),
            Err(OwnerFault::Source)
        );
    }
}

#[test]
fn warm_identity_or_wrong_owned_pid_cannot_initialize_control() {
    for (mut initial, pid) in [(body(), 999), (body(), 101)] {
        if pid == 101 {
            initial["core_boundary"] = serde_json::json!(1);
        }
        let server = Server::new(vec![(200, initial.to_string())]);
        assert!(matches!(
            HustSourceControl::connect_cold(&server.origin, TOKEN.into(), "replica-a", pid),
            Err(OwnerFault::Source)
        ));
    }
    for origin in [
        "http://example.com",
        "https://127.0.0.1:1",
        "http://127.0.0.1:1/path",
        "http://user@127.0.0.1:1",
    ] {
        assert!(matches!(
            HustSourceControl::connect_cold(origin, TOKEN.into(), "replica-a", 101),
            Err(OwnerFault::Source)
        ));
    }
}

#[test]
fn retirement_identity_fields_are_bounded_monotonic_and_mode_stable() {
    let invalid = [
        ("reserved_children", serde_json::json!(257)),
        ("acknowledged", serde_json::json!(1)),
        ("retired_through", serde_json::json!(1)),
        ("ownership_mode", serde_json::json!("trust_prefix")),
    ];
    for (field, value) in invalid {
        let mut initial = body();
        initial[field] = value;
        let server = Server::new(vec![(200, initial.to_string())]);
        assert!(matches!(
            HustSourceControl::connect_cold(
                &server.origin,
                TOKEN.into(),
                "replica-a",
                101
            ),
            Err(OwnerFault::Source)
        ));
    }

    let mut first = body();
    first["ownership_mode"] = serde_json::json!("exclusive_router_v1");
    let mut changed = first.clone();
    changed["ownership_mode"] = serde_json::json!("observe_only");
    let server = Server::new(vec![(200, first.to_string()), (200, changed.to_string())]);
    let mut control =
        HustSourceControl::connect_cold(&server.origin, TOKEN.into(), "replica-a", 101)
            .unwrap();
    assert_eq!(
        control.verify(&spec(&server.origin)),
        Err(OwnerFault::Source)
    );
}
