// SPDX-License-Identifier: Apache-2.0
use crate::{
    hust_terminal_transport::HustTerminalTransport,
    runtime_store::TerminalCursor,
    terminal_driver::{Ownership, RpcFault, TerminalTransport},
};
use serde_json::{json, Value};
use std::{
    collections::VecDeque,
    io::{Read, Write},
    net::TcpListener,
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc, Mutex,
    },
    thread,
    time::{Duration, Instant},
};

struct Server {
    origin: String,
    requests: Arc<Mutex<Vec<String>>>,
    stop: Arc<AtomicBool>,
    thread: Option<thread::JoinHandle<()>>,
}
impl Server {
    fn new(replies: Vec<(String, Duration)>) -> Self {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        listener.set_nonblocking(true).unwrap();
        let origin = format!("http://{}", listener.local_addr().unwrap());
        let stop = Arc::new(AtomicBool::new(false));
        let requests = Arc::new(Mutex::new(Vec::new()));
        let stopped = stop.clone();
        let observed = requests.clone();
        let thread = thread::spawn(move || {
            let mut replies: VecDeque<_> = replies.into();
            while !stopped.load(Ordering::Acquire) {
                let (mut stream, _) = match listener.accept() {
                    Ok(value) => value,
                    Err(error) if error.kind() == std::io::ErrorKind::WouldBlock => {
                        thread::sleep(Duration::from_millis(1));
                        continue;
                    }
                    Err(error) => panic!("listener: {error}"),
                };
                stream
                    .set_read_timeout(Some(Duration::from_secs(1)))
                    .unwrap();
                let mut bytes = Vec::new();
                let mut byte = [0];
                while bytes.len() < 8192 && !bytes.ends_with(b"\r\n\r\n") {
                    match stream.read(&mut byte) {
                        Ok(1) => bytes.push(byte[0]),
                        _ => break,
                    }
                }
                let request = String::from_utf8(bytes).unwrap();
                if request.starts_with("POST ") {
                    let mut body = [0; 2];
                    stream.read_exact(&mut body).unwrap();
                    assert_eq!(&body, b"{}");
                }
                observed.lock().unwrap().push(request);
                let (reply, delay) = replies.pop_front().unwrap_or_default();
                if let Some((head, body)) = reply.split_once("\r\n\r\n") {
                    let _ = write!(stream, "{head}\r\n\r\n");
                    thread::sleep(delay);
                    let _ = stream.write_all(body.as_bytes());
                }
            }
        });
        Self {
            origin,
            requests,
            stop,
            thread: Some(thread),
        }
    }
    fn transport(&self, mode: Ownership, deadline: Duration) -> HustTerminalTransport {
        HustTerminalTransport::new(self.cursor(0), "t".repeat(32), 100, 200, mode, deadline)
            .unwrap()
    }
    fn cursor(&self, through: u64) -> TerminalCursor {
        TerminalCursor {
            source_id: "worker-a".into(),
            worker_url: self.origin.clone(),
            instance_epoch: "instance-a".into(),
            observer_epoch: "observer-a".into(),
            through,
        }
    }
}
impl Drop for Server {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::Release);
        self.thread.take().unwrap().join().unwrap();
    }
}
fn binding() -> Value {
    json!({"worker_id":"worker-a", "instance_epoch":"instance-a", "observer_epoch":"observer-a"})
}
fn family(id: &str) -> Value {
    json!({"attempt_id":id,"expected":1,"bound":1,"ended":1,"sealed":true,
        "abandoned":false,"core_statuses":{"FINISHED_ABORTED":1}})
}
fn reply(after: u64) -> Value {
    json!({"schema_version":1,"binding":binding(),"frontend_pid":100,"core_pid":200,
        "boundary":after+1,"mode":"replay","ownership_mode":"exclusive_router_v1",
        "disabled":false,"records":[{"binding":binding(),"sequence":after+1,"attempt_id":"attempt-a"}],
        "families":[family("attempt-a")]})
}
fn response(value: Value) -> (String, Duration) {
    let body = value.to_string();
    (
        format!(
            "HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
            body.len()
        ),
        Duration::ZERO,
    )
}

#[tokio::test]
async fn ack_accepts_concurrent_new_records_without_advancing_poll_cursor() {
    let server = Server::new(vec![response(reply(1)), response(reply(1))]);
    let mut transport = server.transport(Ownership::ExclusiveRouterV1, Duration::from_secs(1));
    transport.acknowledge(&server.cursor(1)).await.unwrap();
    assert!(transport.last_poll_statuses().is_empty());
    let batch = transport.poll(&server.cursor(1)).await.unwrap();
    assert_eq!(batch.after.through, 1);
    assert_eq!(batch.records[0].sequence, 2);
    assert_eq!(
        transport.last_poll_statuses().get("FINISHED_ABORTED"),
        Some(&1)
    );
    let requests = server.requests.lock().unwrap();
    assert!(requests[0].contains("after=1&acknowledge=1"));
    assert!(requests[1].contains("after=1&acknowledge=0"));
    assert!(requests[0]
        .to_ascii_lowercase()
        .contains(&format!("x-prefix-routing-control: {}", "t".repeat(32))));
}

#[tokio::test]
async fn reject_identity_gaps_partial_families_and_unknown_fields_before_exposing_batch() {
    let changes: Vec<(&str, Value)> = vec![
        ("/binding/observer_epoch", json!("different")),
        ("/frontend_pid", json!(101)),
        ("/core_pid", json!(201)),
        ("/mode", json!("snapshot")),
        ("/ownership_mode", json!("observe_only")),
        ("/disabled", json!(true)),
        ("/boundary", json!(2)),
        ("/records/0/sequence", json!(2)),
        ("/records/0/binding/instance_epoch", json!("wrong")),
        ("/families/0/sealed", json!(false)),
        ("/families/0/abandoned", json!(true)),
        ("/families/0/expected", json!(2)),
        ("/families/0/core_statuses", json!({"NOT_ENDED":1})),
        ("/families/0/core_statuses", json!({"FINISHED_ABORTED":2})),
        ("/families/0/attempt_id", json!("unrelated")),
    ];
    let mut payloads = Vec::new();
    for (path, value) in changes {
        let mut body = reply(0);
        *body.pointer_mut(path).unwrap() = value;
        payloads.push(response(body));
    }
    let mut extra = reply(0);
    extra["unexpected"] = json!(1);
    payloads.push(response(extra));
    let mut duplicate = reply(0);
    duplicate["families"]
        .as_array_mut()
        .unwrap()
        .push(family("attempt-a"));
    payloads.push(response(duplicate));
    let count = payloads.len();
    let server = Server::new(payloads);
    let mut transport = server.transport(Ownership::ExclusiveRouterV1, Duration::from_secs(1));
    for _ in 0..count {
        assert_eq!(
            transport.poll(&server.cursor(0)).await.unwrap_err(),
            RpcFault::InvalidSource
        );
    }
}

#[tokio::test]
async fn all_family_budget_and_duplicate_attempts_are_checked() {
    let mut excessive = reply(0);
    excessive["families"] = json!([family("attempt-a"), {
        "attempt_id":"pending","expected":256,"bound":0,"ended":0,
        "sealed":false,"abandoned":false,"core_statuses":{}}]);
    let mut duplicate = reply(0);
    duplicate["boundary"] = json!(2);
    duplicate["records"]
        .as_array_mut()
        .unwrap()
        .push(json!({"binding":binding(),"sequence":2,"attempt_id":"attempt-a"}));
    let mut overflow = reply(0);
    overflow["boundary"] = json!(u64::MAX);
    let server = Server::new(vec![
        response(excessive),
        response(duplicate),
        response(overflow),
    ]);
    let mut transport = server.transport(Ownership::ExclusiveRouterV1, Duration::from_secs(1));
    assert_eq!(
        transport.poll(&server.cursor(0)).await.unwrap_err(),
        RpcFault::InvalidSource
    );
    assert_eq!(
        transport.poll(&server.cursor(0)).await.unwrap_err(),
        RpcFault::InvalidSource
    );
    assert_eq!(
        transport.poll(&server.cursor(u64::MAX)).await.unwrap_err(),
        RpcFault::InvalidSource
    );
}

#[tokio::test]
async fn response_limits_status_errors_and_body_deadline_are_bounded() {
    let raw = |status, body: &str| {
        (
            format!(
                "HTTP/1.1 {status}\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",
                body.len()
            ),
            Duration::ZERO,
        )
    };
    let server = Server::new(vec![
        raw("503 Unavailable", ""),
        raw("409 Conflict", ""),
        raw("302 Found", ""),
        raw("200 OK", "{"),
        (
            "HTTP/1.1 200 OK\r\nContent-Length: 1048577\r\nConnection: close\r\n\r\n".into(),
            Duration::ZERO,
        ),
        (
            "HTTP/1.1 200 OK\r\nConnection: close\r\n\r\n".to_owned() + &"x".repeat(1_048_577),
            Duration::ZERO,
        ),
        (
            "HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}".into(),
            Duration::from_millis(150),
        ),
    ]);
    let mut transport = server.transport(Ownership::ExclusiveRouterV1, Duration::from_secs(2));
    for expected in [
        RpcFault::Unavailable,
        RpcFault::InvalidSource,
        RpcFault::InvalidSource,
        RpcFault::InvalidSource,
        RpcFault::InvalidSource,
        RpcFault::InvalidSource,
    ] {
        assert_eq!(
            transport.poll(&server.cursor(0)).await.unwrap_err(),
            expected
        );
    }
    let mut short = server.transport(Ownership::ExclusiveRouterV1, Duration::from_millis(80));
    let start = Instant::now();
    assert_eq!(
        short.poll(&server.cursor(0)).await.unwrap_err(),
        RpcFault::Unavailable
    );
    assert!(start.elapsed() < Duration::from_secs(1));
}

#[tokio::test]
async fn retire_is_exact_idempotent_and_forbidden_in_observe_only() {
    let retirement = json!({"schema_version":1,"binding":binding(),"retired":1,
        "retired_through":1,"family_count":0,"reserved_children":0});
    let mut repeated = retirement.clone();
    repeated["retired"] = json!(0);
    let mut wrong = repeated.clone();
    wrong["retired_through"] = json!(2);
    let server = Server::new(vec![
        response(retirement),
        response(repeated),
        response(wrong),
    ]);
    let mut observer = server.transport(Ownership::ObserveOnly, Duration::from_secs(1));
    assert_eq!(
        observer.retire(&server.cursor(1)).await,
        Err(RpcFault::InvalidSource)
    );
    assert!(server.requests.lock().unwrap().is_empty());
    let mut transport = server.transport(Ownership::ExclusiveRouterV1, Duration::from_secs(1));
    transport.retire(&server.cursor(1)).await.unwrap();
    transport.retire(&server.cursor(1)).await.unwrap();
    assert_eq!(
        transport.retire(&server.cursor(1)).await,
        Err(RpcFault::InvalidSource)
    );
    assert!(server.requests.lock().unwrap()[0].contains("through=1&checkpointed_through=1"));
}

#[tokio::test]
async fn invalid_configuration_and_mismatched_cursor_do_not_send() {
    let server = Server::new(vec![]);
    for origin in [
        "https://127.0.0.1:1",
        "http://example.org",
        "http://127.0.0.1/path",
        "http://u:p@127.0.0.1",
    ] {
        let mut cursor = server.cursor(0);
        cursor.worker_url = origin.into();
        assert!(HustTerminalTransport::new(
            cursor,
            "t".repeat(32),
            100,
            200,
            Ownership::ObserveOnly,
            Duration::from_secs(1)
        )
        .is_err());
    }
    let mut transport = server.transport(Ownership::ExclusiveRouterV1, Duration::from_secs(1));
    let mut cursor = server.cursor(0);
    cursor.observer_epoch = "wrong".into();
    assert_eq!(
        transport.poll(&cursor).await.unwrap_err(),
        RpcFault::InvalidSource
    );
    assert!(server.requests.lock().unwrap().is_empty());
    for (token, frontend, core, deadline) in [
        ("weak".to_owned(), 100, 200, Duration::from_secs(1)),
        ("t".repeat(32) + "\n", 100, 200, Duration::from_secs(1)),
        ("t".repeat(32), 0, 200, Duration::from_secs(1)),
        ("t".repeat(32), 100, 100, Duration::from_secs(1)),
        ("t".repeat(32), 100, 200, Duration::ZERO),
    ] {
        assert!(HustTerminalTransport::new(
            server.cursor(0),
            token,
            frontend,
            core,
            Ownership::ObserveOnly,
            deadline
        )
        .is_err());
    }
}
