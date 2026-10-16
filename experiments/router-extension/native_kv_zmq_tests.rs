// SPDX-License-Identifier: Apache-2.0
//! Real Linux IPC fixtures for the shared transport and original-index owner.
use super::*;
use crate::{
    directory::{Binding, Limits},
    native_kv_owner::{NativeKvOwner, SourceControl},
    native_kv_wire::WireFault,
    runtime_cache_port::{CacheFault, InProcessCache, MonotonicClock, PartitionSpec},
    runtime_owner::CachePort,
};
use sha2::{Digest, Sha256};
use std::{
    collections::VecDeque,
    fs,
    os::unix::fs::DirBuilderExt,
    path::PathBuf,
    sync::{atomic::AtomicBool, mpsc, Mutex},
    thread::{self, JoinHandle},
};

type Frames = Vec<Vec<u8>>;
type ReplayPlan = Option<Vec<Frames>>;
static FIXTURE_ID: AtomicU64 = AtomicU64::new(0);
const IDENTITY: &str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";

fn end() -> Frames {
    vec![vec![], vec![], vec![0xff; 8], vec![]]
}

fn live(sequence: u64) -> Frames {
    let payload = rmp_serde::to_vec_named(&serde_json::json!([
        1.0, [{"type": "AllBlocksCleared"}], 0
    ]))
    .unwrap();
    vec![vec![], sequence.to_be_bytes().to_vec(), payload]
}

fn replay(sequence: u64) -> Frames {
    let mut frames = vec![vec![]];
    frames.extend(live(sequence));
    frames
}

struct Peer {
    publisher: Option<zmq::Socket>,
    thread: Option<JoinHandle<()>>,
    stop: Arc<AtomicBool>,
    requests: Arc<Mutex<Vec<u64>>>,
    context: Arc<zmq::Context>,
    root: PathBuf,
    live_endpoint: String,
    replay_endpoint: String,
}

impl Peer {
    fn new(plans: Vec<ReplayPlan>) -> Self {
        let root = std::env::temp_dir().join(format!(
            "prm-kv-{}-{}",
            std::process::id(),
            FIXTURE_ID.fetch_add(1, Ordering::Relaxed)
        ));
        fs::DirBuilder::new().mode(0o700).create(&root).unwrap();
        let live_endpoint = format!("ipc://{}/live", root.display());
        let replay_endpoint = format!("ipc://{}/replay", root.display());
        let context = Arc::new(zmq::Context::new());
        // XPUB provides a subscription barrier: no timing sleep or PUB loss.
        let publisher = context.socket(zmq::XPUB).unwrap();
        publisher.set_linger(0).unwrap();
        publisher.set_sndtimeo(2000).unwrap();
        publisher.bind(&live_endpoint).unwrap();
        let stop = Arc::new(AtomicBool::new(false));
        let requests = Arc::new(Mutex::new(Vec::new()));
        let (ready_tx, ready_rx) = mpsc::sync_channel(1);
        let worker_context = context.clone();
        let worker_stop = stop.clone();
        let worker_requests = requests.clone();
        let worker_endpoint = replay_endpoint.clone();
        let thread = thread::spawn(move || {
            let socket = worker_context.socket(zmq::ROUTER).unwrap();
            socket.set_linger(0).unwrap();
            socket.set_sndtimeo(2000).unwrap();
            socket.bind(&worker_endpoint).unwrap();
            let mut plans = VecDeque::from(plans);
            ready_tx.send(()).unwrap();
            while !worker_stop.load(Ordering::Acquire) {
                if socket.poll(zmq::POLLIN, 25).unwrap() == 0 {
                    continue;
                }
                let request = socket.recv_multipart(zmq::DONTWAIT).unwrap();
                assert_eq!(request.len(), 3);
                assert!(request[1].is_empty());
                let start = u64::from_be_bytes(request[2].as_slice().try_into().unwrap());
                worker_requests.lock().unwrap().push(start);
                if let Some(responses) = plans.pop_front().unwrap_or(Some(vec![end()])) {
                    for response in responses {
                        let mut frames = vec![request[0].clone()];
                        frames.extend(response);
                        socket.send_multipart(frames, 0).unwrap();
                    }
                }
            }
        });
        ready_rx.recv_timeout(Duration::from_secs(3)).unwrap();
        Self {
            publisher: Some(publisher),
            thread: Some(thread),
            stop,
            requests,
            context,
            root,
            live_endpoint,
            replay_endpoint,
        }
    }

    fn connect(&self, deadline: Duration) -> ZmqNativeTransport {
        let transport = ZmqNativeTransport::connect(
            self.context.clone(),
            &self.live_endpoint,
            &self.replay_endpoint,
            128,
            deadline,
        )
        .unwrap();
        let publisher = self.publisher.as_ref().unwrap();
        assert_ne!(publisher.poll(zmq::POLLIN, 2000).unwrap(), 0);
        assert_eq!(publisher.recv_bytes(zmq::DONTWAIT).unwrap(), vec![1]);
        transport
    }

    fn send(&self, frames: Frames) {
        self.publisher
            .as_ref()
            .unwrap()
            .send_multipart(frames, 0)
            .unwrap();
    }
}

impl Drop for Peer {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::Release);
        let joined = self.thread.take().unwrap().join();
        self.publisher.take();
        // Only remove the two fixture-owned endpoints and its private directory.
        let _ = fs::remove_file(self.root.join("live"));
        let _ = fs::remove_file(self.root.join("replay"));
        let _ = fs::remove_dir(&self.root);
        if !thread::panicking() {
            joined.unwrap();
        }
    }
}

#[derive(Debug)]
struct Control;

impl SourceControl for Control {
    fn verify(&mut self, _spec: &PartitionSpec) -> Result<(), OwnerFault> {
        Ok(())
    }
}

fn spec(instance: u64) -> PartitionSpec {
    PartitionSpec {
        binding: Binding {
            worker: Arc::from("http://worker-a"),
            instance,
            publisher: Arc::from(format!("publisher-{instance}")),
            identity: Arc::from(IDENTITY),
            block_size: 128,
        },
        instance_epoch: Arc::from(format!("instance-{instance}")),
    }
}

fn cache() -> Arc<InProcessCache> {
    Arc::new(InProcessCache::new(1, 16384, Arc::new(MonotonicClock::default())).unwrap())
}

fn bootstrap(
    peer: &Peer,
    cache: Arc<InProcessCache>,
    spec: PartitionSpec,
    previous: Option<&PartitionSpec>,
) -> Result<NativeKvOwner<ZmqNativeTransport, Control>, OwnerFault> {
    NativeKvOwner::bootstrap(
        cache,
        spec,
        previous,
        Limits {
            records: 64,
            operations: 64,
            queued_batches: 8,
            queue_bytes: 4096,
            total_bytes: 16384,
            query_blocks: 64,
            parent_depth: 64,
        },
        10_000_000_000,
        8,
        peer.connect(Duration::from_millis(500)),
        Control,
    )
}

fn lookup(cache: &InProcessCache, spec: &PartitionSpec) -> Option<u32> {
    CachePort::lookup(
        cache,
        &spec.binding.worker,
        &spec.instance_epoch,
        IDENTITY,
        &["b".repeat(64)],
    )
}

fn poll(owner: &mut NativeKvOwner<ZmqNativeTransport, Control>) -> Result<bool, OwnerFault> {
    let deadline = Instant::now() + Duration::from_secs(2);
    loop {
        let result = owner.poll_once(Duration::from_millis(25));
        if result != Ok(false) {
            return result;
        }
        assert!(Instant::now() < deadline, "live IPC fixture did not arrive");
    }
}

#[test]
fn actual_replay_live_duplicate_stop_and_replacement() {
    let peer = Peer::new(vec![Some(vec![end()]), Some(vec![replay(0), end()])]);
    let cache = cache();
    let old = spec(1);
    let mut owner = bootstrap(&peer, cache.clone(), old.clone(), None).unwrap();
    assert_eq!(*peer.requests.lock().unwrap(), vec![0, 0]);
    assert_eq!(owner.next_sequence(), 1);
    assert_eq!(lookup(&cache, &old), Some(0));
    peer.send(live(1));
    assert_eq!(poll(&mut owner), Ok(true));
    peer.send(live(1));
    assert_eq!(poll(&mut owner), Ok(true));
    assert_eq!(owner.next_sequence(), 2);
    owner.refresh().unwrap();
    assert_eq!(*peer.requests.lock().unwrap(), vec![0, 0, 2]);

    let other_peer = Peer::new(vec![]);
    let current = spec(2);
    let mut replacement =
        bootstrap(&other_peer, cache.clone(), current.clone(), Some(&old)).unwrap();
    assert!(matches!(
        owner.shutdown(),
        Err(OwnerFault::Cache(CacheFault::StalePartition))
    ));
    drop(owner);
    assert_eq!(lookup(&cache, &current), Some(0));
    replacement.shutdown().unwrap();
    assert_eq!(lookup(&cache, &current), None);
}

#[test]
fn actual_fixed_hust_store_matches_original_index_then_clear_removes_it() {
    let peer = Peer::new(vec![]);
    let cache = cache();
    let spec = spec(1);
    let mut owner = bootstrap(&peer, cache.clone(), spec.clone(), None).unwrap();
    let encoded = include_str!("../router-index/fixtures/hust-cedb477-msgspec-v1.hex").trim();
    let payload = (0..encoded.len())
        .step_by(2)
        .map(|offset| u8::from_str_radix(&encoded[offset..offset + 2], 16).unwrap())
        .collect();
    peer.send(vec![vec![], 0_u64.to_be_bytes().to_vec(), payload]);
    assert_eq!(poll(&mut owner), Ok(true));
    let query: Vec<String> = [0_u32, 128]
        .into_iter()
        .map(|start| {
            let mut digest = Sha256::new();
            for token in start..start + 128 {
                digest.update(token.to_le_bytes());
            }
            format!("{:x}", digest.finalize())
        })
        .collect();
    let lookup = || {
        CachePort::lookup(
            &*cache,
            &spec.binding.worker,
            &spec.instance_epoch,
            IDENTITY,
            &query,
        )
    };
    assert_eq!(lookup(), Some(2));
    peer.send(live(1));
    assert_eq!(poll(&mut owner), Ok(true));
    assert_eq!(lookup(), Some(0));
    owner.shutdown().unwrap();
    assert_eq!(lookup(), None);
}

#[test]
fn absent_replay_peer_cannot_block_send_past_deadline() {
    struct Sink;
    impl ReplaySink for Sink {
        fn accept(&mut self, _: Frames) -> Result<(), OwnerFault> {
            panic!("absent peer cannot produce a batch");
        }
    }
    let peer = Peer::new(vec![]);
    let mut transport = ZmqNativeTransport::connect(
        peer.context.clone(),
        &peer.live_endpoint,
        &format!("ipc://{}/absent", peer.root.display()),
        1,
        Duration::from_millis(100),
    )
    .unwrap();
    let started = Instant::now();
    assert_eq!(transport.replay(0, &mut Sink), Err(OwnerFault::Transport));
    assert!(started.elapsed() < Duration::from_secs(1));
}

#[test]
fn actual_gap_malformed_and_aggregate_overflow_fence_directory() {
    let mut extra_frame = live(0);
    extra_frame.push(vec![]);
    // Individual frames are under MAXMSGSIZE but the aggregate exceeds budget.
    let aggregate = vec![vec![0; 40], vec![0; 40], vec![0; MAX_PAYLOAD]];
    for frames in [live(1), extra_frame, aggregate, vec![vec![], vec![0; 8]]] {
        let peer = Peer::new(vec![]);
        let cache = cache();
        let spec = spec(1);
        let mut owner = bootstrap(&peer, cache.clone(), spec.clone(), None).unwrap();
        peer.send(frames);
        assert!(poll(&mut owner).is_err());
        assert_eq!(lookup(&cache, &spec), None);
        assert!(owner.poll_once(Duration::ZERO).is_err());
    }
}

#[test]
fn actual_conflicting_duplicate_fences_directory() {
    let peer = Peer::new(vec![]);
    let cache = cache();
    let spec = spec(1);
    let mut owner = bootstrap(&peer, cache.clone(), spec.clone(), None).unwrap();
    peer.send(live(0));
    assert_eq!(poll(&mut owner), Ok(true));
    let mut conflict = live(0);
    conflict[2] = rmp_serde::to_vec_named(&serde_json::json!([
        1.0, [{"type": "AllBlocksCleared"}, {"type": "AllBlocksCleared"}], 0
    ]))
    .unwrap();
    peer.send(conflict);
    assert_eq!(poll(&mut owner), Err(OwnerFault::Wire(WireFault::Conflict)));
    assert_eq!(lookup(&cache, &spec), None);
}

#[test]
fn actual_publisher_disconnect_withdraws_trust() {
    let mut peer = Peer::new(vec![]);
    let cache = cache();
    let spec = spec(1);
    let mut owner = bootstrap(&peer, cache.clone(), spec.clone(), None).unwrap();
    peer.publisher.take();
    assert_eq!(poll(&mut owner), Err(OwnerFault::Transport));
    assert_eq!(lookup(&cache, &spec), None);
}

#[test]
fn actual_replay_timeout_and_batch_limit_are_bounded() {
    let mut excessive = (0..129).map(replay).collect::<Vec<_>>();
    excessive.push(end());
    for response in [None, Some(excessive)] {
        let peer = Peer::new(vec![Some(vec![end()]), Some(vec![end()]), response]);
        let cache = cache();
        let spec = spec(1);
        let mut owner = bootstrap(&peer, cache.clone(), spec.clone(), None).unwrap();
        let started = Instant::now();
        assert_eq!(owner.refresh(), Err(OwnerFault::Transport));
        assert!(started.elapsed() < Duration::from_secs(2));
        assert_eq!(lookup(&cache, &spec), None);
    }
}

#[test]
fn actual_warm_origin_rejected_without_pool_allocation() {
    let peer = Peer::new(vec![Some(vec![replay(0), end()])]);
    let cache = cache();
    assert!(matches!(
        bootstrap(&peer, cache.clone(), spec(1), None),
        Err(OwnerFault::WarmOrigin)
    ));
    assert_eq!(cache.reserved_bytes(), Ok(0));
}
