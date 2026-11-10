//! A minimal HTTP/1.1 server for the data plane (review nit d). Connections are read by their
//! own threads (bounded by `Limits`), each request under ONE deadline from its first byte
//! and each keep-alive wait under an idle limit, so a slow or idle peer only ever holds its own
//! connection thread. Fully parsed requests go to a bounded pool of handler workers; a handler
//! panic answers 500 and the worker lives on. Headers and bodies are bounded, keep-alive is
//! supported, chunked bodies are refused. Only what the I4 contract needs.

use std::io::{BufRead, BufReader, Read, Write};
use std::collections::HashMap;
use std::net::{IpAddr, TcpListener, TcpStream};
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::mpsc::{Receiver, SyncSender, sync_channel};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

/// Whole-request budget, header + body, from the request's first byte (re-review item 1).
pub const REQUEST_DEADLINE: Duration = Duration::from_secs(5);
/// How long a keep-alive connection may wait for its next request.
pub const KEEPALIVE_IDLE: Duration = Duration::from_secs(5);
/// Connection and memory limits (follow-up items 2 and 3). Every limit is enforced before
/// authentication, so an unauthenticated peer can never exceed it.
#[derive(Debug, Clone, Copy)]
pub struct Limits {
    /// concurrent connections; beyond this an accepted connection is closed at once
    pub max_connections: usize,
    /// concurrent connections from one source IP. Lambda reaches the engine through shared
    /// Hyperplane ENIs (one per subnet + security group), so the whole interceptor fleet
    /// arrives from at most a few IPs: size this as the fleet's per-subnet connection need.
    pub max_per_peer: usize,
    /// request-body bytes held at once across all connections (parsed, queued or being
    /// handled); a request that would exceed it is answered 503 before its body is read
    pub max_inflight_body_bytes: usize,
}

impl Default for Limits {
    fn default() -> Self {
        Limits { max_connections: 1024, max_per_peer: 256, max_inflight_body_bytes: 64 << 20 }
    }
}
pub const MAX_HEADER_BYTES: usize = 16 * 1024;
pub const MAX_HEADERS: usize = 64;
pub const MAX_BODY_BYTES: usize = 1 << 20;
const MAX_REQUESTS_PER_CONNECTION: usize = 10_000;

pub struct Request {
    pub method: String,
    pub path: String,
    pub headers: Vec<(String, String)>,
    pub body: Vec<u8>,
}

impl Request {
    /// The value of a header that appears exactly once; `""` when absent OR repeated, so a
    /// duplicated `X-LP-*` header can never be read two ways (it simply fails auth).
    pub fn header(&self, name: &str) -> &str {
        let mut found = self.headers.iter().filter(|(k, _)| k.eq_ignore_ascii_case(name));
        match (found.next(), found.next()) {
            (Some((_, v)), None) => v.as_str(),
            _ => "",
        }
    }
}

pub type Handler = dyn Fn(&Request) -> (u16, String) + Send + Sync;

enum Parsed {
    Request(Request, bool, BodyReservation),
    Closed,
    Bad(u16, &'static str),
}

/// A socket whose every read is bounded by one absolute deadline: a peer dribbling a byte at a
/// time cannot stretch a request past it (a per-read timeout alone would let it).
pub struct DeadlineStream {
    inner: TcpStream,
    deadline: Instant,
}

impl Read for DeadlineStream {
    fn read(&mut self, buf: &mut [u8]) -> std::io::Result<usize> {
        let left = self.deadline.saturating_duration_since(Instant::now());
        if left.is_zero() {
            return Err(std::io::Error::new(std::io::ErrorKind::TimedOut, "request deadline"));
        }
        self.inner.set_read_timeout(Some(left))?;
        self.inner.read(buf)
    }
}

type Reader = BufReader<DeadlineStream>;

fn read_line(reader: &mut Reader, budget: &mut usize) -> std::io::Result<Option<String>> {
    let mut line = Vec::new();
    let n = reader.by_ref().take(*budget as u64 + 1).read_until(b'\n', &mut line)?;
    if n == 0 {
        return Ok(None);
    }
    if n > *budget || !line.ends_with(b"\n") {
        return Err(std::io::Error::new(std::io::ErrorKind::InvalidData, "header too large"));
    }
    *budget -= n;
    while line.last().is_some_and(|b| *b == b'\n' || *b == b'\r') {
        line.pop();
    }
    String::from_utf8(line).map(Some).map_err(|_| std::io::Error::new(std::io::ErrorKind::InvalidData, "not UTF-8"))
}

/// An oversized header is answered; a read timeout or reset just closes (nothing to tell an
/// idle or vanished peer).
fn too_large_or_closed(e: std::io::Error) -> Parsed {
    if e.kind() == std::io::ErrorKind::InvalidData {
        Parsed::Bad(431, "Request Header Fields Too Large")
    } else {
        Parsed::Closed
    }
}

/// Bytes of one request's body counted against `max_inflight_body_bytes` until dropped.
struct BodyReservation(Arc<AtomicUsize>, usize);

impl Drop for BodyReservation {
    fn drop(&mut self) {
        self.0.fetch_sub(self.1, Ordering::SeqCst);
    }
}

fn reserve(inflight: &Arc<AtomicUsize>, bytes: usize, budget: usize) -> Option<BodyReservation> {
    let before = inflight.fetch_add(bytes, Ordering::SeqCst);
    if before + bytes > budget {
        inflight.fetch_sub(bytes, Ordering::SeqCst);
        return None;
    }
    Some(BodyReservation(inflight.clone(), bytes))
}

fn parse(reader: &mut Reader, inflight: &Arc<AtomicUsize>, limits: &Limits) -> Parsed {
    // wait (idle limit) for the next request's first byte, then one deadline for all of it
    reader.get_mut().deadline = Instant::now() + KEEPALIVE_IDLE;
    match reader.fill_buf() {
        Ok([]) | Err(_) => return Parsed::Closed,
        Ok(_) => {}
    }
    reader.get_mut().deadline = Instant::now() + REQUEST_DEADLINE;
    let mut budget = MAX_HEADER_BYTES;
    let request_line = match read_line(reader, &mut budget) {
        Ok(Some(l)) if !l.is_empty() => l,
        Ok(_) => return Parsed::Closed,
        Err(e) => return too_large_or_closed(e),
    };
    let mut parts = request_line.split(' ');
    let (Some(method), Some(target), Some(version), None) = (parts.next(), parts.next(), parts.next(), parts.next())
    else {
        return Parsed::Bad(400, "Bad Request");
    };
    if !matches!(version, "HTTP/1.0" | "HTTP/1.1") {
        return Parsed::Bad(505, "HTTP Version Not Supported");
    }
    let mut headers = Vec::new();
    loop {
        match read_line(reader, &mut budget) {
            Ok(Some(l)) if l.is_empty() => break,
            Ok(Some(l)) => {
                let Some((k, v)) = l.split_once(':') else { return Parsed::Bad(400, "Bad Request") };
                // no obs-fold, no whitespace in or around a field name (request smuggling)
                if k.is_empty() || !k.bytes().all(|b| b.is_ascii_alphanumeric() || b"!#$%&'*+-.^_`|~".contains(&b)) {
                    return Parsed::Bad(400, "Bad Request");
                }
                if headers.len() >= MAX_HEADERS {
                    return Parsed::Bad(431, "Request Header Fields Too Large");
                }
                headers.push((k.trim().to_string(), v.trim().to_string()));
            }
            Ok(None) => return Parsed::Closed,
            Err(e) => return too_large_or_closed(e),
        }
    }
    let req = Request { method: method.to_string(), path: target.split('?').next().unwrap_or("").to_string(), headers, body: vec![] };
    // the only framing is one plain Content-Length: any Transfer-Encoding, a repeated
    // Content-Length or a non-digit length is refused, so no proxy can frame it differently
    if req.headers.iter().any(|(k, _)| k.eq_ignore_ascii_case("Transfer-Encoding")) {
        return Parsed::Bad(411, "Length Required");
    }
    let lengths: Vec<&str> =
        req.headers.iter().filter(|(k, _)| k.eq_ignore_ascii_case("Content-Length")).map(|(_, v)| v.as_str()).collect();
    let length: usize = match lengths.as_slice() {
        [] => 0,
        [v] if !v.is_empty() && v.len() <= 7 && v.bytes().all(|b| b.is_ascii_digit()) => v.parse().unwrap(),
        [v] if v.bytes().all(|b| b.is_ascii_digit()) && !v.is_empty() => return Parsed::Bad(413, "Payload Too Large"),
        _ => return Parsed::Bad(400, "Bad Request"),
    };
    if length > MAX_BODY_BYTES {
        return Parsed::Bad(413, "Payload Too Large");
    }
    let Some(reservation) = reserve(inflight, length, limits.max_inflight_body_bytes) else {
        return Parsed::Bad(503, "Service Unavailable");
    };
    let mut body = vec![0; length];
    if reader.read_exact(&mut body).is_err() {
        return Parsed::Closed;
    }
    // the unauthenticated health probe never holds a connection open (follow-up item 2)
    let keep_alive = version == "HTTP/1.1"
        && !req.header("Connection").eq_ignore_ascii_case("close")
        && !(req.method == "GET" && req.path == "/healthz");
    Parsed::Request(Request { body, ..req }, keep_alive, reservation)
}

fn reason(status: u16) -> &'static str {
    match status {
        200 => "OK",
        400 => "Bad Request",
        401 => "Unauthorized",
        404 => "Not Found",
        409 => "Conflict",
        411 => "Length Required",
        413 => "Payload Too Large",
        431 => "Request Header Fields Too Large",
        500 => "Internal Server Error",
        503 => "Service Unavailable",
        505 => "HTTP Version Not Supported",
        _ => "Error",
    }
}

fn write_response(stream: &mut TcpStream, status: u16, body: &str, keep_alive: bool) -> std::io::Result<()> {
    let head = format!(
        "HTTP/1.1 {status} {}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: {}\r\n\r\n",
        reason(status),
        body.len(),
        if keep_alive { "keep-alive" } else { "close" }
    );
    stream.write_all(head.as_bytes())?;
    stream.write_all(body.as_bytes())?;
    stream.flush()
}

type Job = (Request, SyncSender<(u16, String)>);

fn serve(stream: TcpStream, jobs: &SyncSender<Job>, inflight: &Arc<AtomicUsize>, limits: &Limits) {
    if stream.set_write_timeout(Some(REQUEST_DEADLINE)).is_err() {
        return;
    }
    let _ = stream.set_nodelay(true);
    let Ok(mut writer) = stream.try_clone() else { return };
    let mut reader = BufReader::new(DeadlineStream { inner: stream, deadline: Instant::now() + KEEPALIVE_IDLE });
    for _ in 0..MAX_REQUESTS_PER_CONNECTION {
        match parse(&mut reader, inflight, limits) {
            Parsed::Closed => return,
            Parsed::Bad(status, text) => {
                let _ = write_response(&mut writer, status, &format!("{{\"code\":\"bad_request\",\"message\":\"{text}\"}}"), false);
                return;
            }
            Parsed::Request(req, keep_alive, reservation) => {
                let (reply_tx, reply_rx) = sync_channel(1);
                if jobs.send((req, reply_tx)).is_err() {
                    return;
                }
                let Ok((status, body)) = reply_rx.recv() else { return };
                drop(reservation); // the request (and its body) is gone once answered
                if write_response(&mut writer, status, &body, keep_alive).is_err() || !keep_alive {
                    return;
                }
            }
        }
    }
}

/// Run one request; a panic is answered 500 and leaves the worker alive (re-review item 2).
pub fn call(handler: &Handler, req: &Request) -> (u16, String) {
    match std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| handler(req))) {
        Ok(reply) => reply,
        Err(_) => (500, "{\"code\":\"internal\",\"message\":\"handler panicked\"}".into()),
    }
}

/// One accepted connection, counted globally and per source IP until it closes.
struct ConnectionSlot {
    open: Arc<AtomicUsize>,
    peers: Arc<Mutex<HashMap<IpAddr, usize>>>,
    peer: IpAddr,
}

impl ConnectionSlot {
    fn take(open: &Arc<AtomicUsize>, peers: &Arc<Mutex<HashMap<IpAddr, usize>>>, peer: IpAddr, limits: &Limits) -> Option<Self> {
        let mut map = peers.lock().unwrap_or_else(|p| p.into_inner());
        let n = map.entry(peer).or_insert(0);
        if *n >= limits.max_per_peer || open.load(Ordering::SeqCst) >= limits.max_connections {
            if *n == 0 {
                map.remove(&peer);
            }
            return None;
        }
        *n += 1;
        open.fetch_add(1, Ordering::SeqCst);
        Some(ConnectionSlot { open: open.clone(), peers: peers.clone(), peer })
    }
}

impl Drop for ConnectionSlot {
    fn drop(&mut self) {
        self.open.fetch_sub(1, Ordering::SeqCst);
        let mut map = self.peers.lock().unwrap_or_else(|p| p.into_inner());
        if let Some(n) = map.get_mut(&self.peer) {
            *n -= 1;
            if *n == 0 {
                map.remove(&self.peer);
            }
        }
    }
}

/// Accept on `listener`: one reader thread per connection (at most `MAX_CONNECTIONS`), and
/// `workers` handler threads behind a bounded queue.
pub fn run(listener: TcpListener, workers: usize, limits: Limits, handler: Arc<Handler>) -> std::io::Result<()> {
    let (jobs, rx) = sync_channel::<Job>(workers.max(1) * 4);
    let rx: Arc<Mutex<Receiver<Job>>> = Arc::new(Mutex::new(rx));
    for _ in 0..workers.max(1) {
        let rx = rx.clone();
        let handler = handler.clone();
        std::thread::spawn(move || loop {
            // the queue lock guards no data: recover it rather than lose the worker
            let next = rx.lock().unwrap_or_else(|p| p.into_inner()).recv();
            match next {
                Ok((req, reply)) => {
                    let _ = reply.send(call(handler.as_ref(), &req));
                }
                Err(_) => return,
            }
        });
    }
    let open = Arc::new(AtomicUsize::new(0));
    let peers = Arc::new(Mutex::new(HashMap::new()));
    let inflight = Arc::new(AtomicUsize::new(0));
    for stream in listener.incoming() {
        match stream {
            Ok(s) => {
                let Ok(peer) = s.peer_addr().map(|a| a.ip()) else { continue };
                let Some(slot) = ConnectionSlot::take(&open, &peers, peer, &limits) else {
                    drop(s); // over a limit: closed at once, before any byte is read
                    continue;
                };
                let jobs = jobs.clone();
                let inflight = inflight.clone();
                let spawned = std::thread::Builder::new().stack_size(256 * 1024).spawn(move || {
                    let _slot = slot;
                    serve(s, &jobs, &inflight, &limits);
                });
                if let Err(e) = spawned {
                    eprintln!("launchpad-policy-engine: connection thread: {e}");
                }
            }
            Err(e) => eprintln!("launchpad-policy-engine: accept: {e}"),
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn start(workers: usize, handler: Arc<Handler>) -> std::net::SocketAddr {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let addr = listener.local_addr().unwrap();
        std::thread::spawn(move || run(listener, workers, Limits::default(), handler));
        addr
    }

    fn get(addr: std::net::SocketAddr, path: &str) -> String {
        let mut s = TcpStream::connect(addr).unwrap();
        s.set_read_timeout(Some(Duration::from_secs(10))).unwrap();
        write!(s, "GET {path} HTTP/1.1\r\nConnection: close\r\n\r\n").unwrap();
        let mut out = String::new();
        let _ = s.read_to_string(&mut out);
        out
    }

    #[test]
    fn a_handler_panic_answers_500_and_the_worker_survives() {
        let handler: Arc<Handler> = Arc::new(|req: &Request| {
            if req.path == "/panic" {
                panic!("boom");
            }
            (200, "{}".into())
        });
        let addr = start(1, handler);
        for _ in 0..3 {
            assert!(get(addr, "/panic").starts_with("HTTP/1.1 500 "));
        }
        // the single worker is still there
        assert!(get(addr, "/ok").starts_with("HTTP/1.1 200 "));
    }

    #[test]
    fn slow_peers_do_not_stall_the_workers_and_are_cut_at_the_deadline() {
        let addr = start(2, Arc::new(|_: &Request| (200, "{}".into())));
        // 4 peers dribbling one header byte every 1.5 s (each read well inside any per-read
        // timeout): with workers = 2 they would hold every worker indefinitely
        let started = Instant::now();
        let peers: Vec<_> = (0..4)
            .map(|_| {
                std::thread::spawn(move || {
                    let mut s = TcpStream::connect(addr).unwrap();
                    for b in b"GET /x HTTP/1.1\r\nX-Slow: aaaaaaaaaaaaaaaaaaaaa".iter() {
                        if s.write_all(&[*b]).is_err() {
                            return Instant::now();
                        }
                        std::thread::sleep(Duration::from_millis(1500));
                        let mut probe = [0u8; 1];
                        s.set_nonblocking(true).unwrap();
                        let closed = matches!(s.read(&mut probe), Ok(0));
                        s.set_nonblocking(false).unwrap();
                        if closed {
                            return Instant::now();
                        }
                    }
                    Instant::now()
                })
            })
            .collect();
        std::thread::sleep(Duration::from_millis(500));
        for _ in 0..5 {
            let t = Instant::now();
            assert!(get(addr, "/ok").starts_with("HTTP/1.1 200 "));
            assert!(t.elapsed() < Duration::from_secs(1), "stalled behind slow peers: {:?}", t.elapsed());
        }
        for p in peers {
            let cut = p.join().unwrap().duration_since(started);
            assert!(cut < REQUEST_DEADLINE + Duration::from_secs(3), "slow peer held for {cut:?}");
        }
    }

    #[test]
    fn an_idle_keep_alive_connection_is_closed_after_the_idle_limit() {
        let addr = start(1, Arc::new(|_: &Request| (200, "{}".into())));
        let mut s = TcpStream::connect(addr).unwrap();
        s.set_read_timeout(Some(Duration::from_secs(15))).unwrap();
        write!(s, "GET /a HTTP/1.1\r\n\r\n").unwrap();
        let mut buf = [0u8; 4096];
        let mut got = Vec::new();
        while !got.ends_with(b"\r\n\r\n{}") {
            let n = s.read(&mut buf).unwrap();
            assert!(n > 0, "closed before the response");
            got.extend_from_slice(&buf[..n]);
        }
        let t = Instant::now();
        assert_eq!(s.read(&mut buf).unwrap(), 0, "idle keep-alive not closed");
        assert!(t.elapsed() < KEEPALIVE_IDLE + Duration::from_secs(2));
    }
}

#[cfg(test)]
mod limit_tests {
    use super::*;

    fn start(limits: Limits) -> std::net::SocketAddr {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let addr = listener.local_addr().unwrap();
        let handler: Arc<Handler> = Arc::new(|req: &Request| {
            if req.path == "/slow" {
                std::thread::sleep(Duration::from_millis(1500));
            }
            (200, "{}".into())
        });
        std::thread::spawn(move || run(listener, 4, limits, handler));
        addr
    }

    fn closed_at_once(s: &mut TcpStream) -> bool {
        s.set_read_timeout(Some(Duration::from_secs(2))).unwrap();
        let mut b = [0u8; 16];
        matches!(s.read(&mut b), Ok(0)) || matches!(s.read(&mut b), Err(e) if e.kind() == std::io::ErrorKind::ConnectionReset)
    }

    fn roundtrip(addr: std::net::SocketAddr, raw: &[u8]) -> String {
        let mut s = TcpStream::connect(addr).unwrap();
        s.set_read_timeout(Some(Duration::from_secs(8))).unwrap();
        s.write_all(raw).unwrap();
        let mut out = String::new();
        let _ = s.read_to_string(&mut out);
        out
    }

    #[test]
    fn one_peer_cannot_take_more_than_its_connection_cap() {
        let addr = start(Limits { max_per_peer: 2, ..Limits::default() });
        let held: Vec<_> = (0..2).map(|_| TcpStream::connect(addr).unwrap()).collect();
        std::thread::sleep(Duration::from_millis(200));
        let mut third = TcpStream::connect(addr).unwrap();
        assert!(closed_at_once(&mut third), "a third connection from the same peer was kept");
        drop(held);
        std::thread::sleep(Duration::from_millis(200));
        let out = roundtrip(addr, b"GET /ok HTTP/1.1\r\nConnection: close\r\n\r\n");
        assert!(out.starts_with("HTTP/1.1 200 "), "slots not released: {out}");
    }

    #[test]
    fn healthz_never_keeps_the_connection_open() {
        let addr = start(Limits::default());
        let t = Instant::now();
        let out = roundtrip(addr, b"GET /healthz HTTP/1.1\r\n\r\n"); // keep-alive by default
        assert!(out.contains("Connection: close"), "{out}");
        assert!(t.elapsed() < Duration::from_secs(2), "connection held open after /healthz");
    }

    #[test]
    fn bodies_held_in_flight_are_bounded() {
        let addr = start(Limits { max_inflight_body_bytes: 100, ..Limits::default() });
        let body = "x".repeat(80);
        let slow = format!("POST /slow HTTP/1.1\r\nContent-Length: 80\r\nConnection: close\r\n\r\n{body}");
        let first = std::thread::spawn(move || roundtrip(addr, slow.as_bytes()));
        std::thread::sleep(Duration::from_millis(300));
        let second = roundtrip(addr, format!("POST /x HTTP/1.1\r\nContent-Length: 80\r\nConnection: close\r\n\r\n{}", "y".repeat(80)).as_bytes());
        assert!(second.starts_with("HTTP/1.1 503 "), "budget not enforced: {second}");
        assert!(first.join().unwrap().starts_with("HTTP/1.1 200 "));
        // released after the first answered
        let third = roundtrip(addr, format!("POST /x HTTP/1.1\r\nContent-Length: 80\r\nConnection: close\r\n\r\n{}", "z".repeat(80)).as_bytes());
        assert!(third.starts_with("HTTP/1.1 200 "), "{third}");
    }
}
