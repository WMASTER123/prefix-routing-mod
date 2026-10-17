// SPDX-License-Identifier: Apache-2.0
//! Single-owner trust and recovery state for the fixed HUST native KV stream.
//!
//! The transport and source-control adapters are explicit ports. Constructing
//! this type performs no device work and package import never starts a thread
//! or opens a socket. A caller may run it on one dedicated blocking task.
use crate::{
    directory::Limits,
    native_kv_wire::{decode_live, decode_replay, Acceptance, Continuity, WireFault},
    runtime_cache_port::{CacheFault, InProcessCache, PartitionSpec},
};
use std::{fmt, sync::Arc, time::Duration};

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum OwnerFault {
    Source,
    Transport,
    WarmOrigin,
    Wire(WireFault),
    Cache(CacheFault),
    Stopped,
}

impl From<WireFault> for OwnerFault {
    fn from(value: WireFault) -> Self {
        Self::Wire(value)
    }
}

impl From<CacheFault> for OwnerFault {
    fn from(value: CacheFault) -> Self {
        Self::Cache(value)
    }
}

/// Read-only identity/liveness proof. It must describe the exact engine
/// instance represented by `PartitionSpec`; it never mutates the engine.
pub trait SourceControl: fmt::Debug + Send {
    fn verify(&mut self, spec: &PartitionSpec) -> Result<(), OwnerFault>;
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum LivePoll {
    Idle,
    Frames(Vec<Vec<u8>>),
    Disconnected,
}

pub trait ReplaySink {
    fn accept(&mut self, frames: Vec<Vec<u8>>) -> Result<(), OwnerFault>;
}

/// The production adapter owns exactly one SUB and one DEALER for one
/// partition. `replay` must return only after a valid END frame and enforce its
/// own message-count/deadline limits before calling the sink.
pub trait NativeTransport: fmt::Debug + Send {
    fn replay(&mut self, start: u64, sink: &mut dyn ReplaySink) -> Result<(), OwnerFault>;
    fn receive(&mut self, timeout: Duration) -> Result<LivePoll, OwnerFault>;
}

struct ApplySink<'a> {
    cache: &'a InProcessCache,
    spec: &'a PartitionSpec,
    continuity: &'a mut Continuity,
}

impl ReplaySink for ApplySink<'_> {
    fn accept(&mut self, frames: Vec<Vec<u8>>) -> Result<(), OwnerFault> {
        let Some(batch) = decode_replay(&frames)? else {
            return Err(OwnerFault::Transport);
        };
        match self.continuity.accept(&batch)? {
            Acceptance::Apply => self.cache.live(self.spec, batch)?,
            Acceptance::Duplicate => {}
        }
        Ok(())
    }
}

struct RejectWarm;

impl ReplaySink for RejectWarm {
    fn accept(&mut self, frames: Vec<Vec<u8>>) -> Result<(), OwnerFault> {
        if decode_replay(&frames)?.is_some() {
            return Err(OwnerFault::WarmOrigin);
        }
        Err(OwnerFault::Transport)
    }
}

/// Owns trust for one exact partition. Any ambiguous source, transport, wire,
/// continuity, or index outcome permanently fences this owner. Ordinary live
/// frames can never restore trust; recovery requires constructing a new owner
/// from a newly verified controlled origin.
pub struct NativeKvOwner<T, C> {
    cache: Arc<InProcessCache>,
    spec: PartitionSpec,
    transport: T,
    control: C,
    continuity: Continuity,
    failed: Option<OwnerFault>,
    fenced: bool,
}

impl<T: fmt::Debug, C: fmt::Debug> fmt::Debug for NativeKvOwner<T, C> {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("NativeKvOwner")
            .field("spec", &self.spec)
            .field("next", &self.continuity.next())
            .field("failed", &self.failed)
            .field("fenced", &self.fenced)
            .finish_non_exhaustive()
    }
}

impl<T: NativeTransport, C: SourceControl> NativeKvOwner<T, C> {
    #[allow(clippy::too_many_arguments)]
    pub fn bootstrap(
        cache: Arc<InProcessCache>,
        spec: PartitionSpec,
        previous: Option<&PartitionSpec>,
        limits: Limits,
        lease: u64,
        window: usize,
        mut transport: T,
        mut control: C,
    ) -> Result<Self, OwnerFault> {
        // Validate caller-controlled bounds before registering or replacing a
        // partition so a bad configuration cannot consume pool capacity.
        let continuity = Continuity::new(window)?;
        // Native replay has no authoritative snapshot. The only supported
        // origin is an owned engine proven empty before requests are admitted.
        control.verify(&spec)?;
        transport.replay(0, &mut RejectWarm)?;
        control.verify(&spec)?;

        let token = match previous {
            Some(old) => cache.replace(old, spec.clone(), limits, lease)?,
            None => cache.register(spec.clone(), limits, lease)?,
        };
        if let Err(error) = cache.install(&token, vec![], 0, 0) {
            let _ = cache.unregister(&spec);
            return Err(error.into());
        }

        let mut owner = Self {
            cache,
            spec,
            transport,
            control,
            continuity,
            failed: None,
            fenced: false,
        };
        if let Err(error) = owner.refresh() {
            owner.fail(error.clone());
            return Err(error);
        }
        Ok(owner)
    }

    pub fn next_sequence(&self) -> u64 {
        self.continuity.next()
    }

    pub fn failure(&self) -> Option<&OwnerFault> {
        self.failed.as_ref()
    }

    pub fn refresh(&mut self) -> Result<(), OwnerFault> {
        self.available()?;
        let token = match self.cache.begin_control(&self.spec) {
            Ok(token) => token,
            Err(error) => {
                let error = OwnerFault::Cache(error);
                self.fail(error.clone());
                return Err(error);
            }
        };
        let result: Result<(), OwnerFault> = (|| {
            self.control.verify(&self.spec)?;
            let start = self.continuity.next();
            let mut sink = ApplySink {
                cache: &self.cache,
                spec: &self.spec,
                continuity: &mut self.continuity,
            };
            self.transport.replay(start, &mut sink)?;
            self.control.verify(&self.spec)?;
            self.cache.confirm_control(&token, self.continuity.next())?;
            Ok(())
        })();
        if let Err(error) = result {
            self.fail(error.clone());
            return Err(error);
        }
        Ok(())
    }

    pub fn poll_once(&mut self, timeout: Duration) -> Result<bool, OwnerFault> {
        self.available()?;
        let result: Result<bool, OwnerFault> = (|| match self.transport.receive(timeout)? {
            LivePoll::Idle => Ok(false),
            LivePoll::Disconnected => Err(OwnerFault::Transport),
            LivePoll::Frames(frames) => {
                let batch = decode_live(&frames)?;
                match self.continuity.accept(&batch)? {
                    Acceptance::Apply => self.cache.live(&self.spec, batch)?,
                    Acceptance::Duplicate => {}
                }
                Ok(true)
            }
        })();
        if let Err(error) = result {
            self.fail(error.clone());
            return Err(error);
        }
        result
    }

    pub fn shutdown(&mut self) -> Result<(), OwnerFault> {
        if self.failed.is_none() {
            self.failed = Some(OwnerFault::Stopped);
        }
        self.fence()
    }

    fn available(&self) -> Result<(), OwnerFault> {
        match &self.failed {
            Some(error) => Err(error.clone()),
            None => Ok(()),
        }
    }

    fn fail(&mut self, error: OwnerFault) {
        if self.failed.is_none() {
            self.failed = Some(error);
        }
        let _ = self.fence();
    }

    fn fence(&mut self) -> Result<(), OwnerFault> {
        if !self.fenced {
            self.cache.disconnect(&self.spec)?;
            self.fenced = true;
        }
        Ok(())
    }
}

impl<T, C> Drop for NativeKvOwner<T, C> {
    fn drop(&mut self) {
        if !self.fenced {
            let _ = self.cache.disconnect(&self.spec);
            self.fenced = true;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::{directory::Binding, runtime_cache_port::MonotonicClock, runtime_owner::CachePort};
    use serde::{Serialize, Serializer};
    use sha2::{Digest, Sha256};
    use std::collections::VecDeque;

    const IDENTITY: &str = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa";

    #[derive(Debug)]
    struct Binary(Vec<u8>);

    impl Serialize for Binary {
        fn serialize<S>(&self, serializer: S) -> Result<S::Ok, S::Error>
        where
            S: Serializer,
        {
            serializer.serialize_bytes(&self.0)
        }
    }

    #[derive(Serialize)]
    struct Stored<'a> {
        #[serde(rename = "type")]
        kind: &'a str,
        block_hashes: Vec<Binary>,
        parent_block_hash: Option<Binary>,
        token_ids: Vec<u32>,
        block_size: u32,
        lora_id: Option<u32>,
        medium: Option<&'a str>,
        lora_name: Option<&'a str>,
        extra_keys: Option<Vec<Option<u32>>>,
        group_idx: Option<u32>,
        kv_cache_spec_kind: Option<&'a str>,
        kv_cache_spec_sliding_window: Option<u32>,
        locality: Option<&'a str>,
        ownership: Option<&'a str>,
        session_id: Option<&'a str>,
    }

    #[derive(Serialize)]
    struct Envelope<T>(f64, Vec<T>, u32);

    #[derive(Debug)]
    struct Control {
        replies: VecDeque<Result<(), OwnerFault>>,
    }

    impl SourceControl for Control {
        fn verify(&mut self, _spec: &PartitionSpec) -> Result<(), OwnerFault> {
            self.replies.pop_front().unwrap_or(Ok(()))
        }
    }

    #[derive(Debug)]
    struct Transport {
        replays: VecDeque<Result<Vec<Vec<Vec<u8>>>, OwnerFault>>,
        live: VecDeque<Result<LivePoll, OwnerFault>>,
    }

    impl NativeTransport for Transport {
        fn replay(&mut self, _start: u64, sink: &mut dyn ReplaySink) -> Result<(), OwnerFault> {
            for frames in self.replays.pop_front().unwrap_or(Ok(vec![]))? {
                sink.accept(frames)?;
            }
            Ok(())
        }

        fn receive(&mut self, _timeout: Duration) -> Result<LivePoll, OwnerFault> {
            self.live.pop_front().unwrap_or(Ok(LivePoll::Idle))
        }
    }

    fn make_spec(instance: u64, epoch: &str) -> PartitionSpec {
        PartitionSpec {
            binding: Binding {
                worker: Arc::from("http://worker-a"),
                instance,
                publisher: Arc::from(format!("publisher-{instance}")),
                identity: Arc::from(IDENTITY),
                block_size: 128,
            },
            instance_epoch: Arc::from(epoch),
        }
    }

    fn limits() -> Limits {
        Limits {
            records: 64,
            operations: 64,
            queued_batches: 8,
            queue_bytes: 4096,
            total_bytes: 16384,
            query_blocks: 64,
            parent_depth: 64,
        }
    }

    fn digest(tokens: &[u32]) -> String {
        let mut value = Sha256::new();
        for token in tokens {
            value.update(token.to_le_bytes());
        }
        format!("{:x}", value.finalize())
    }

    fn payload(hash: u8, start: u32) -> Vec<u8> {
        rmp_serde::to_vec_named(&Envelope(
            1.0,
            vec![Stored {
                kind: "BlockStored",
                block_hashes: vec![Binary(vec![hash; 32])],
                parent_block_hash: (start != 0).then(|| Binary(vec![hash.saturating_sub(1); 32])),
                token_ids: (start..start + 128).collect(),
                block_size: 128,
                lora_id: None,
                medium: Some("GPU"),
                lora_name: None,
                extra_keys: None,
                group_idx: Some(0),
                kv_cache_spec_kind: Some("full_attention"),
                kv_cache_spec_sliding_window: None,
                locality: Some("LOCAL"),
                ownership: None,
                session_id: Some("fixture"),
            }],
            0,
        ))
        .unwrap()
    }

    fn live(sequence: u64, hash: u8, start: u32) -> Vec<Vec<u8>> {
        vec![
            vec![],
            sequence.to_be_bytes().to_vec(),
            payload(hash, start),
        ]
    }

    fn replay(sequence: u64, hash: u8, start: u32) -> Vec<Vec<u8>> {
        let mut frames = vec![vec![]];
        frames.extend(live(sequence, hash, start));
        frames
    }

    fn make_cache() -> Arc<InProcessCache> {
        Arc::new(InProcessCache::new(1, 16384, Arc::new(MonotonicClock::default())).unwrap())
    }

    fn make_owner(
        cache: Arc<InProcessCache>,
        spec: PartitionSpec,
        transport: Transport,
        control: Control,
        previous: Option<&PartitionSpec>,
    ) -> Result<NativeKvOwner<Transport, Control>, OwnerFault> {
        NativeKvOwner::bootstrap(
            cache,
            spec,
            previous,
            limits(),
            10_000_000_000,
            8,
            transport,
            control,
        )
    }

    fn lookup(cache: &InProcessCache, spec: &PartitionSpec, locals: &[String]) -> Option<u32> {
        CachePort::lookup(
            cache,
            &spec.binding.worker,
            &spec.instance_epoch,
            &spec.binding.identity,
            locals,
        )
    }

    #[test]
    fn bootstrap_replay_live_duplicate_and_shutdown_are_fail_closed() {
        let cache = make_cache();
        let spec = make_spec(1, "epoch-a");
        let first = replay(0, 1, 0);
        let live_second = live(1, 2, 128);
        let transport = Transport {
            replays: VecDeque::from([Ok(vec![]), Ok(vec![first])]),
            live: VecDeque::from([
                Ok(LivePoll::Frames(live_second.clone())),
                Ok(LivePoll::Frames(live_second)),
            ]),
        };
        let control = Control {
            replies: VecDeque::new(),
        };
        let mut owner = make_owner(cache.clone(), spec.clone(), transport, control, None).unwrap();
        assert_eq!(owner.next_sequence(), 1);
        assert_eq!(
            lookup(&cache, &spec, &[digest(&(0..128).collect::<Vec<_>>())]),
            Some(1)
        );
        assert_eq!(owner.poll_once(Duration::ZERO), Ok(true));
        assert_eq!(owner.poll_once(Duration::ZERO), Ok(true));
        assert_eq!(owner.next_sequence(), 2);
        assert_eq!(
            lookup(
                &cache,
                &spec,
                &[
                    digest(&(0..128).collect::<Vec<_>>()),
                    digest(&(128..256).collect::<Vec<_>>()),
                ],
            ),
            Some(2)
        );
        owner.shutdown().unwrap();
        assert_eq!(
            lookup(&cache, &spec, &[digest(&(0..128).collect::<Vec<_>>())]),
            None
        );
    }

    #[test]
    fn gap_disconnect_and_control_failure_withdraw_trust() {
        let cache = make_cache();
        let spec = make_spec(1, "epoch-a");
        let transport = Transport {
            replays: VecDeque::from([Ok(vec![]), Ok(vec![])]),
            live: VecDeque::from([Ok(LivePoll::Frames(live(1, 2, 128)))]),
        };
        let control = Control {
            replies: VecDeque::new(),
        };
        let mut owner = make_owner(cache.clone(), spec.clone(), transport, control, None).unwrap();
        assert!(matches!(
            owner.poll_once(Duration::ZERO),
            Err(OwnerFault::Wire(WireFault::Gap))
        ));
        assert_eq!(
            lookup(&cache, &spec, &[digest(&(0..128).collect::<Vec<_>>())]),
            None
        );

        let second_cache = make_cache();
        let second_spec = make_spec(1, "epoch-a");
        let second_transport = Transport {
            replays: VecDeque::from([Ok(vec![]), Ok(vec![])]),
            live: VecDeque::new(),
        };
        let second_control = Control {
            replies: VecDeque::from([Ok(()), Ok(()), Err(OwnerFault::Source)]),
        };
        assert!(matches!(
            make_owner(
                second_cache.clone(),
                second_spec.clone(),
                second_transport,
                second_control,
                None,
            ),
            Err(OwnerFault::Source)
        ));
        assert_eq!(
            lookup(
                &second_cache,
                &second_spec,
                &[digest(&(0..128).collect::<Vec<_>>())],
            ),
            None
        );

        let disconnected_cache = make_cache();
        let disconnected_spec = make_spec(1, "epoch-a");
        let disconnected_transport = Transport {
            replays: VecDeque::from([Ok(vec![]), Ok(vec![])]),
            live: VecDeque::from([Ok(LivePoll::Disconnected)]),
        };
        let disconnected_control = Control {
            replies: VecDeque::new(),
        };
        let mut disconnected_owner = make_owner(
            disconnected_cache.clone(),
            disconnected_spec.clone(),
            disconnected_transport,
            disconnected_control,
            None,
        )
        .unwrap();
        assert_eq!(
            disconnected_owner.poll_once(Duration::ZERO),
            Err(OwnerFault::Transport)
        );
        assert_eq!(
            lookup(
                &disconnected_cache,
                &disconnected_spec,
                &[digest(&(0..128).collect::<Vec<_>>())],
            ),
            None
        );
    }

    #[test]
    fn warm_origin_is_rejected_before_reserving_partition() {
        let cache = make_cache();
        let spec = make_spec(1, "epoch-a");
        let transport = Transport {
            replays: VecDeque::from([Ok(vec![replay(0, 1, 0)])]),
            live: VecDeque::new(),
        };
        let control = Control {
            replies: VecDeque::new(),
        };
        assert!(matches!(
            make_owner(cache.clone(), spec, transport, control, None),
            Err(OwnerFault::WarmOrigin)
        ));
        assert_eq!(cache.reserved_bytes(), Ok(0));
    }

    #[test]
    fn stale_old_owner_cannot_fence_replacement() {
        let cache = make_cache();
        let old = make_spec(1, "epoch-a");
        let transport = || Transport {
            replays: VecDeque::from([Ok(vec![]), Ok(vec![])]),
            live: VecDeque::new(),
        };
        let control = || Control {
            replies: VecDeque::new(),
        };
        let mut old_owner =
            make_owner(cache.clone(), old.clone(), transport(), control(), None).unwrap();
        let current = make_spec(2, "epoch-b");
        let current_owner = make_owner(
            cache.clone(),
            current.clone(),
            transport(),
            control(),
            Some(&old),
        )
        .unwrap();
        assert_eq!(
            lookup(&cache, &current, &[digest(&(0..128).collect::<Vec<_>>())],),
            Some(0)
        );
        assert!(matches!(
            old_owner.refresh(),
            Err(OwnerFault::Cache(CacheFault::StalePartition))
        ));
        assert!(matches!(
            old_owner.shutdown(),
            Err(OwnerFault::Cache(CacheFault::StalePartition))
        ));
        assert_eq!(
            lookup(&cache, &current, &[digest(&(0..128).collect::<Vec<_>>())],),
            Some(0)
        );
        drop(current_owner);
        assert_eq!(
            lookup(&cache, &current, &[digest(&(0..128).collect::<Vec<_>>())],),
            None
        );
    }
}
