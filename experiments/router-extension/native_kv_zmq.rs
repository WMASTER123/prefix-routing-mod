// SPDX-License-Identifier: Apache-2.0
//! Bounded libzmq transport for `NativeKvOwner`.
//!
//! Construction is explicit and restricted to private same-host IPC. The
//! caller owns task/thread lifecycle; this module never spawns work itself.
use crate::{
    native_kv_owner::{LivePoll, NativeTransport, OwnerFault, ReplaySink},
    native_kv_wire::MAX_PAYLOAD,
};
use std::{
    fmt,
    sync::{atomic::AtomicU64, atomic::Ordering, Arc},
    time::{Duration, Instant},
};

const MAX_ENDPOINT: usize = 100;
const MAX_FRAME_TOTAL: usize = MAX_PAYLOAD + 64;
const MAX_REPLAY_BATCHES: usize = 128;
static MONITOR_ID: AtomicU64 = AtomicU64::new(0);

fn endpoint(value: &str) -> Result<(), OwnerFault> {
    if !value.starts_with("ipc:///") || value.len() > MAX_ENDPOINT || value.as_bytes().contains(&0)
    {
        return Err(OwnerFault::Transport);
    }
    Ok(())
}

fn timeout_ms(value: Duration) -> i64 {
    value.as_millis().min(i64::MAX as u128) as i64
}

fn receive_frames(socket: &zmq::Socket, expected: usize) -> Result<Vec<Vec<u8>>, OwnerFault> {
    let mut frames = Vec::with_capacity(expected);
    let mut total = 0usize;
    for index in 0..expected {
        let frame = socket.recv_bytes(0).map_err(|_| OwnerFault::Transport)?;
        total = total
            .checked_add(frame.len())
            .ok_or(OwnerFault::Transport)?;
        if total > MAX_FRAME_TOTAL {
            return Err(OwnerFault::Transport);
        }
        let more = socket.get_rcvmore().map_err(|_| OwnerFault::Transport)?;
        frames.push(frame);
        if !more {
            return if index + 1 == expected {
                Ok(frames)
            } else {
                Err(OwnerFault::Transport)
            };
        }
    }
    // There is at least one unconsumed frame. The owner will fail closed and
    // drop this transport; do not try to resynchronize attacker-shaped input.
    Err(OwnerFault::Transport)
}

pub struct ZmqNativeTransport {
    subscriber: zmq::Socket,
    dealer: zmq::Socket,
    monitor: zmq::Socket,
    replay_deadline: Duration,
    // Keep the context last so sockets are dropped before the final Arc.
    _context: Arc<zmq::Context>,
}

impl fmt::Debug for ZmqNativeTransport {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter
            .debug_struct("ZmqNativeTransport")
            .field("replay_deadline", &self.replay_deadline)
            .finish_non_exhaustive()
    }
}

impl ZmqNativeTransport {
    pub fn connect(
        context: Arc<zmq::Context>,
        live_endpoint: &str,
        replay_endpoint: &str,
        receive_hwm: i32,
        replay_deadline: Duration,
    ) -> Result<Self, OwnerFault> {
        endpoint(live_endpoint)?;
        endpoint(replay_endpoint)?;
        if !(1..=4096).contains(&receive_hwm)
            || replay_deadline.is_zero()
            || replay_deadline > Duration::from_secs(30)
        {
            return Err(OwnerFault::Transport);
        }

        let subscriber = context
            .socket(zmq::SUB)
            .map_err(|_| OwnerFault::Transport)?;
        let dealer = context
            .socket(zmq::DEALER)
            .map_err(|_| OwnerFault::Transport)?;
        let monitor = context
            .socket(zmq::PAIR)
            .map_err(|_| OwnerFault::Transport)?;
        for socket in [&subscriber, &dealer, &monitor] {
            socket.set_linger(0).map_err(|_| OwnerFault::Transport)?;
            socket
                .set_maxmsgsize(MAX_PAYLOAD as i64)
                .map_err(|_| OwnerFault::Transport)?;
            socket
                .set_rcvhwm(receive_hwm)
                .map_err(|_| OwnerFault::Transport)?;
        }
        subscriber
            .set_subscribe(b"")
            .map_err(|_| OwnerFault::Transport)?;

        let monitor_endpoint = format!(
            "inproc://prefix-routing-kv-monitor-{}-{}",
            std::process::id(),
            MONITOR_ID.fetch_add(1, Ordering::Relaxed)
        );
        subscriber
            .monitor(&monitor_endpoint, zmq::SocketEvent::DISCONNECTED as i32)
            .map_err(|_| OwnerFault::Transport)?;
        monitor
            .connect(&monitor_endpoint)
            .map_err(|_| OwnerFault::Transport)?;
        subscriber
            .connect(live_endpoint)
            .map_err(|_| OwnerFault::Transport)?;
        dealer
            .connect(replay_endpoint)
            .map_err(|_| OwnerFault::Transport)?;

        Ok(Self {
            subscriber,
            dealer,
            monitor,
            replay_deadline,
            _context: context,
        })
    }

    fn monitor_disconnected(&self) -> Result<bool, OwnerFault> {
        if self
            .monitor
            .poll(zmq::POLLIN, 0)
            .map_err(|_| OwnerFault::Transport)?
            == 0
        {
            return Ok(false);
        }
        let frames = receive_frames(&self.monitor, 2)?;
        if frames[0].len() < 2 {
            return Err(OwnerFault::Transport);
        }
        let event = u16::from_ne_bytes([frames[0][0], frames[0][1]]);
        Ok(event == zmq::SocketEvent::DISCONNECTED.to_raw())
    }
}

impl NativeTransport for ZmqNativeTransport {
    fn replay(&mut self, start: u64, sink: &mut dyn ReplaySink) -> Result<(), OwnerFault> {
        self.dealer
            .send_multipart([Vec::new(), start.to_be_bytes().to_vec()], 0)
            .map_err(|_| OwnerFault::Transport)?;
        let deadline = Instant::now() + self.replay_deadline;
        let mut batches = 0usize;
        loop {
            if self.monitor_disconnected()? || Instant::now() >= deadline {
                return Err(OwnerFault::Transport);
            }
            let remaining = deadline.saturating_duration_since(Instant::now());
            let wait = timeout_ms(remaining.min(Duration::from_millis(25)));
            if self
                .dealer
                .poll(zmq::POLLIN, wait)
                .map_err(|_| OwnerFault::Transport)?
                == 0
            {
                continue;
            }
            let frames = receive_frames(&self.dealer, 4)?;
            if frames[0].is_empty()
                && frames[1].is_empty()
                && frames[2].as_slice() == [0xff; 8]
                && frames[3].is_empty()
            {
                return Ok(());
            }
            batches = batches.checked_add(1).ok_or(OwnerFault::Transport)?;
            if batches > MAX_REPLAY_BATCHES {
                return Err(OwnerFault::Transport);
            }
            sink.accept(frames)?;
        }
    }

    fn receive(&mut self, timeout: Duration) -> Result<LivePoll, OwnerFault> {
        if self.monitor_disconnected()? {
            return Ok(LivePoll::Disconnected);
        }
        let ready = self
            .subscriber
            .poll(
                zmq::POLLIN,
                timeout_ms(timeout.min(Duration::from_millis(100))),
            )
            .map_err(|_| OwnerFault::Transport)?;
        if self.monitor_disconnected()? {
            return Ok(LivePoll::Disconnected);
        }
        if ready == 0 {
            return Ok(LivePoll::Idle);
        }
        Ok(LivePoll::Frames(receive_frames(&self.subscriber, 3)?))
    }
}

impl Drop for ZmqNativeTransport {
    fn drop(&mut self) {
        let _ = self.subscriber.monitor("", 0);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn endpoints_and_runtime_bounds_fail_before_socket_connect() {
        let context = Arc::new(zmq::Context::new());
        for invalid in [
            "tcp://127.0.0.1:9000",
            "ipc://",
            &format!("ipc:///{}", "x".repeat(MAX_ENDPOINT)),
        ] {
            assert!(matches!(
                ZmqNativeTransport::connect(
                    context.clone(),
                    invalid,
                    "ipc:///tmp/replay.sock",
                    128,
                    Duration::from_secs(2),
                ),
                Err(OwnerFault::Transport)
            ));
        }
        assert!(matches!(
            ZmqNativeTransport::connect(
                context,
                "ipc:///tmp/live.sock",
                "ipc:///tmp/replay.sock",
                0,
                Duration::from_secs(2),
            ),
            Err(OwnerFault::Transport)
        ));
    }
}
