// ipas-pair — pairing-file helper for ipa-source.
//
// Small CLI around the MIT-licensed `idevice` crate. Every command prints
// progress as JSON lines ({"event": ...}) and finishes with exactly one
// result line ({"ok": true, ...} or {"ok": false, "error": "..."}).
// The Python side (app/pairing.py) only ever parses the last line.
//
// Commands:
//   lockdown  --udid U --out FILE [--no-wifi]      USB: lockdown pairing file (+ Wi-Fi debugging)
//   rppair    --udid U --host NAME --out FILE      USB: RPPairing file (iOS 17.4+)
//   apps      (--udid U | --ip IP --pairing FILE)  list user apps (bundle id, names)
//   push      (--udid U | --ip IP --pairing FILE) --bundle ID --path REL --file FILE
//   verify    --ip IP --pairing FILE               Wi-Fi: check a lockdown pairing file
//   rpverify  --ip IP --port N --rp FILE --host NAME   Wi-Fi: check an RPPairing file
//   wifiaccept --name NAME --out FILE [--port N] [--timeout S]
//             iOS 27+: advertise as pairable computer, the iPhone connects, we show the PIN
//   discover  [--timeout S]                        mDNS: _apple-mobdev2 + _remotepairing

use std::{
    collections::HashMap,
    io::Write,
    net::{IpAddr, SocketAddr, SocketAddrV6},
    str::FromStr,
    time::Duration,
};

use idevice::{
    IdeviceService, RemoteXpcClient,
    afc::{AfcClient, opcode::AfcFopenMode},
    core_device_proxy::CoreDeviceProxy,
    house_arrest::HouseArrestClient,
    installation_proxy::InstallationProxyClient,
    lockdown::LockdownClient,
    pairing_file::PairingFile,
    provider::{IdeviceProvider, TcpProvider},
    remote_pairing::{
        PAIRABLE_HOST_SERVICE_TYPE, PairableHost, PairableHostInfo, RemotePairingClient,
        RemotePairingLockdownService, RpPairingFile, RpPairingSocket,
    },
    rsd::RsdHandshake,
    usbmuxd::UsbmuxdAddr,
};
use serde_json::{Value, json};

const LABEL: &str = "ipa-source";
const WIRELESS_DOMAIN: &str = "com.apple.mobile.wireless_lockdown";

type Res<T> = Result<T, String>;

fn emit(v: Value) {
    let mut out = std::io::stdout().lock();
    let _ = writeln!(out, "{v}");
    let _ = out.flush();
}

fn progress(msg: &str) {
    emit(json!({"event": "progress", "message": msg}));
}

struct Args {
    cmd: String,
    flags: HashMap<String, String>,
}

impl Args {
    fn parse() -> Res<Self> {
        let mut it = std::env::args().skip(1);
        let cmd = it.next().ok_or("no command given")?;
        let mut flags = HashMap::new();
        let rest: Vec<String> = it.collect();
        let mut i = 0;
        while i < rest.len() {
            let k = rest[i]
                .strip_prefix("--")
                .ok_or_else(|| format!("unexpected argument {}", rest[i]))?
                .to_string();
            if i + 1 < rest.len() && !rest[i + 1].starts_with("--") {
                flags.insert(k, rest[i + 1].clone());
                i += 2;
            } else {
                flags.insert(k, String::new());
                i += 1;
            }
        }
        Ok(Self { cmd, flags })
    }
    fn get(&self, k: &str) -> Option<&str> {
        self.flags.get(k).map(|s| s.as_str()).filter(|s| !s.is_empty())
    }
    fn req(&self, k: &str) -> Res<&str> {
        self.get(k).ok_or_else(|| format!("--{k} is required"))
    }
    fn has(&self, k: &str) -> bool {
        self.flags.contains_key(k)
    }
}

/// Parses "1.2.3.4", "fe80::1", "fe80::1%5".
fn parse_ip(s: &str) -> Res<(IpAddr, Option<u32>)> {
    let (addr, scope) = match s.split_once('%') {
        Some((a, sc)) => (a, sc.parse::<u32>().ok()),
        None => (s, None),
    };
    let ip = IpAddr::from_str(addr).map_err(|e| format!("invalid IP {s}: {e}"))?;
    Ok((ip, scope))
}

fn sock_addr(ip: IpAddr, scope: Option<u32>, port: u16) -> SocketAddr {
    match ip {
        IpAddr::V6(v6) => SocketAddr::V6(SocketAddrV6::new(v6, port, 0, scope.unwrap_or(0))),
        v4 => SocketAddr::new(v4, port),
    }
}

async fn usb_provider(udid: &str) -> Res<Box<dyn IdeviceProvider>> {
    let addr = UsbmuxdAddr::from_env_var().map_err(|e| format!("bad USBMUXD_SOCKET_ADDRESS: {e}"))?;
    let mut mux = addr
        .connect(0)
        .await
        .map_err(|e| format!("usbmuxd not reachable: {e}"))?;
    let dev = mux
        .get_device(udid)
        .await
        .map_err(|e| format!("device {udid} not found: {e}"))?;
    Ok(Box::new(dev.to_provider(addr, LABEL)))
}

fn read_pairing(path: &str) -> Res<PairingFile> {
    PairingFile::read_from_file(path).map_err(|e| format!("cannot read pairing file: {e}"))
}

async fn provider_from(a: &Args) -> Res<Box<dyn IdeviceProvider>> {
    if let Some(udid) = a.get("udid") {
        return usb_provider(udid).await;
    }
    let (ip, scope) = parse_ip(a.req("ip")?)?;
    let pairing_file = read_pairing(a.req("pairing")?)?;
    Ok(Box::new(TcpProvider { addr: ip, scope_id: scope, pairing_file, label: LABEL.into() }))
}

async fn lockdown_info(ld: &mut LockdownClient) -> Value {
    let mut info = serde_json::Map::new();
    for key in ["DeviceName", "ProductVersion", "ProductType", "WiFiAddress", "UniqueDeviceID", "BuildVersion"] {
        if let Ok(v) = ld.get_value(Some(key), None).await {
            if let Some(s) = v.as_string() {
                info.insert(key.to_string(), json!(s));
            }
        }
    }
    Value::Object(info)
}

// ---------------------------------------------------------------- lockdown

async fn cmd_lockdown(a: &Args) -> Res<Value> {
    let udid = a.req("udid")?;
    let out = a.req("out")?;
    let addr = UsbmuxdAddr::from_env_var().map_err(|e| e.to_string())?;
    let mut mux = addr.connect(0).await.map_err(|e| format!("usbmuxd not reachable: {e}"))?;
    progress("Lese Pair-Record von usbmuxd");
    let mut pf = mux
        .get_pair_record(udid)
        .await
        .map_err(|e| format!("kein Pair-Record – iPhone erst koppeln (Vertrauen): {e}"))?;
    let provider = usb_provider(udid).await?;
    let mut ld = LockdownClient::connect(&*provider).await.map_err(|e| format!("lockdown: {e}"))?;
    ld.start_session(&pf).await.map_err(|e| format!("Pairing ungültig (start_session): {e}"))?;
    let info = lockdown_info(&mut ld).await;

    let mut wifi = Value::Null;
    if !a.has("no-wifi") {
        progress("Aktiviere WLAN-Debugging");
        wifi = match ld
            .set_value("EnableWifiDebugging", plist::Value::Boolean(true), Some(WIRELESS_DOMAIN))
            .await
        {
            Ok(()) => json!(true),
            Err(e) => json!(format!("{e}")),
        };
    }

    pf.udid = Some(udid.to_string());
    let bytes = pf.serialize().map_err(|e| format!("serialize: {e}"))?;
    tokio::fs::write(out, &bytes).await.map_err(|e| format!("write {out}: {e}"))?;
    Ok(json!({"device": info, "wifi_debugging": wifi, "bytes": bytes.len()}))
}

// ---------------------------------------------------------------- rppair

async fn rppair_lockdown(provider: &dyn IdeviceProvider, host: &str) -> Res<RpPairingFile> {
    let service = RemotePairingLockdownService::connect(provider)
        .await
        .map_err(|e| format!("remotepairing lockdown service: {e}"))?;
    let mut client = service.into_client(host).map_err(|e| format!("{e}"))?;
    let mut rpf = RpPairingFile::generate(host);
    client
        .connect(&mut rpf, || async { "000000".to_string() })
        .await
        .map_err(|e| format!("RPPairing (lockdown): {e:?}"))?;
    Ok(rpf)
}

async fn rppair_tunnel(provider: &dyn IdeviceProvider, host: &str) -> Res<RpPairingFile> {
    let proxy = CoreDeviceProxy::connect(provider).await.map_err(|e| format!("CoreDeviceProxy: {e}"))?;
    let rsd_port = proxy.tunnel_info().server_rsd_port;
    let adapter = proxy.create_software_tunnel().map_err(|e| format!("tunnel: {e}"))?;
    let mut adapter = adapter.to_async_handle();
    let rsd = adapter.connect(rsd_port).await.map_err(|e| format!("RSD connect: {e}"))?;
    let hs = RsdHandshake::new(rsd).await.map_err(|e| format!("RSD handshake: {e}"))?;
    let ts = hs
        .services
        .get("com.apple.internal.dt.coredevice.untrusted.tunnelservice")
        .ok_or("untrusted tunnel service missing")?;
    let stream = adapter.connect(ts.port).await.map_err(|e| format!("tunnel service: {e}"))?;
    let mut conn = RemoteXpcClient::new(stream).await.map_err(|e| format!("XPC: {e}"))?;
    conn.do_handshake().await.map_err(|e| format!("XPC handshake: {e}"))?;
    let _ = conn.recv_root().await;
    let mut rpf = RpPairingFile::generate(host);
    let mut rpc = RemotePairingClient::new(conn, host);
    rpc.connect(&mut rpf, || async { "000000".to_string() })
        .await
        .map_err(|e| format!("RPPairing (tunnel): {e:?}"))?;
    Ok(rpf)
}

async fn cmd_rppair(a: &Args) -> Res<Value> {
    let udid = a.req("udid")?;
    let host = a.req("host")?;
    let out = a.req("out")?;
    let provider = usb_provider(udid).await?;
    progress("RPPairing über lockdown");
    let (rpf, via) = match rppair_lockdown(&*provider, host).await {
        Ok(f) => (f, "lockdown"),
        Err(first) => {
            progress(&format!("{first} – versuche CoreDeviceProxy"));
            emit(json!({"event": "trust", "message": "Ggf. am iPhone auf „Vertrauen“ tippen"}));
            match rppair_tunnel(&*provider, host).await {
                Ok(f) => (f, "tunnel"),
                Err(second) => return Err(format!("{first}; {second}")),
            }
        }
    };
    rpf.write_to_file(out).await.map_err(|e| format!("write {out}: {e}"))?;
    Ok(json!({"via": via, "identifier": rpf.identifier}))
}

// ---------------------------------------------------------------- apps / push

async fn cmd_apps(a: &Args) -> Res<Value> {
    let provider = provider_from(a).await?;
    let mut ip = InstallationProxyClient::connect(&*provider)
        .await
        .map_err(|e| format!("installation_proxy: {e}"))?;
    let apps = ip.get_apps(Some("User"), None).await.map_err(|e| format!("Lookup: {e}"))?;
    let s = |d: &plist::Dictionary, k: &str| d.get(k).and_then(|v| v.as_string()).unwrap_or("").to_string();
    let mut list = Vec::new();
    for (bid, v) in apps {
        if let Some(d) = v.as_dictionary() {
            list.push(json!({
                "bundle_id": bid,
                "name": s(d, "CFBundleDisplayName"),
                "bundle_name": s(d, "CFBundleName"),
                "version": s(d, "CFBundleShortVersionString"),
                "file_sharing": d.get("UIFileSharingEnabled").and_then(|v| v.as_boolean()).unwrap_or(false),
            }));
        }
    }
    Ok(json!({"apps": list}))
}

async fn afc_write(afc: &mut AfcClient, path: &str, bytes: &[u8]) -> Res<()> {
    // create parent directories, ignore "exists" errors
    let parts: Vec<&str> = path.trim_start_matches('/').split('/').collect();
    let mut cur = String::new();
    for p in &parts[..parts.len().saturating_sub(1)] {
        cur.push('/');
        cur.push_str(p);
        let _ = afc.mk_dir(cur.clone()).await;
    }
    let mut f = afc
        .open(path.to_string(), AfcFopenMode::WrOnly)
        .await
        .map_err(|e| format!("open {path}: {e}"))?;
    f.write_entire(bytes).await.map_err(|e| format!("write {path}: {e}"))?;
    f.close().await.map_err(|e| format!("close: {e}"))?;
    Ok(())
}

async fn cmd_push(a: &Args) -> Res<Value> {
    let bundle = a.req("bundle")?.to_string();
    let rel = a.req("path")?.trim_start_matches('/').to_string();
    let bytes = tokio::fs::read(a.req("file")?).await.map_err(|e| format!("read: {e}"))?;
    let provider = provider_from(a).await?;
    let target = format!("/Documents/{rel}");

    progress("Öffne Documents der App (house_arrest)");
    let ha = HouseArrestClient::connect(&*provider).await.map_err(|e| format!("house_arrest: {e}"))?;
    let first = match ha.vend_documents(bundle.clone()).await {
        Ok(mut afc) => match afc_write(&mut afc, &target, &bytes).await {
            Ok(()) => return Ok(json!({"path": target, "via": "documents"})),
            Err(e) => e,
        },
        Err(e) => format!("VendDocuments: {e}"),
    };
    progress(&format!("{first} – versuche VendContainer"));
    let ha = HouseArrestClient::connect(&*provider).await.map_err(|e| format!("house_arrest: {e}"))?;
    let mut afc = ha
        .vend_container(bundle)
        .await
        .map_err(|e| format!("{first}; VendContainer: {e}"))?;
    afc_write(&mut afc, &target, &bytes).await.map_err(|e| format!("{first}; {e}"))?;
    Ok(json!({"path": target, "via": "container"}))
}

// ---------------------------------------------------------------- Wi-Fi

async fn cmd_verify(a: &Args) -> Res<Value> {
    let (ip, scope) = parse_ip(a.req("ip")?)?;
    let pf = read_pairing(a.req("pairing")?)?;
    let provider = TcpProvider { addr: ip, scope_id: scope, pairing_file: pf.clone(), label: LABEL.into() };
    let mut ld = tokio::time::timeout(Duration::from_secs(8), LockdownClient::connect(&provider))
        .await
        .map_err(|_| "Zeitüberschreitung – iPhone im WLAN nicht erreichbar (entsperrt? gleiches Netz?)".to_string())?
        .map_err(|e| format!("lockdown: {e}"))?;
    ld.start_session(&pf).await.map_err(|e| format!("Pairing abgelehnt: {e}"))?;
    let info = lockdown_info(&mut ld).await;
    Ok(json!({"device": info}))
}

async fn tcp(a: &Args) -> Res<tokio::net::TcpStream> {
    let (ip, scope) = parse_ip(a.req("ip")?)?;
    let port: u16 = a.req("port")?.parse().map_err(|_| "invalid --port")?;
    tokio::time::timeout(Duration::from_secs(8), tokio::net::TcpStream::connect(sock_addr(ip, scope, port)))
        .await
        .map_err(|_| "Zeitüberschreitung beim Verbinden".to_string())?
        .map_err(|e| format!("connect: {e}"))
}

async fn cmd_rpverify(a: &Args) -> Res<Value> {
    let host = a.req("host")?;
    let mut rpf = RpPairingFile::read_from_file(a.req("rp")?)
        .await
        .map_err(|e| format!("RPPairing-Datei: {e}"))?;
    let stream = tcp(a).await?;
    let mut client = RemotePairingClient::new(RpPairingSocket::new(stream), host);
    client.attempt_pair_verify().await.map_err(|e| format!("handshake: {e}"))?;
    client.validate_pairing(&mut rpf).await.map_err(|e| format!("pair-verify: {e:?}"))?;
    Ok(json!({"valid": true}))
}

/// iOS 27+ device-initiated pairing: we advertise `_remotepairing-pairable-host._tcp`,
/// the iPhone offers "pair with <name>", connects to us and the user types the PIN we show.
async fn cmd_wifiaccept(a: &Args) -> Res<Value> {
    use mdns_sd::{ServiceDaemon, ServiceInfo};
    let name = a.req("name")?.to_string();
    let out = a.req("out")?;
    let port: u16 = a.get("port").and_then(|p| p.parse().ok()).unwrap_or(0);
    let timeout: u64 = a.get("timeout").and_then(|t| t.parse().ok()).unwrap_or(300);

    let listener = tokio::net::TcpListener::bind(("0.0.0.0", port))
        .await
        .map_err(|e| format!("Port {port}: {e}"))?;
    let port = listener.local_addr().map_err(|e| e.to_string())?.port();

    let info = PairableHostInfo::generate(name.clone(), "Mac17,7");
    let mut rpf = RpPairingFile::generate(&name);
    let ident = rpf.identifier.clone();
    let txt = info.mdns_txt_records(&ident);
    let props: Vec<(&str, &str)> = txt.iter().map(|(k, v)| (k.as_str(), v.as_str())).collect();

    let daemon = ServiceDaemon::new().map_err(|e| format!("mDNS: {e}"))?;
    // "remotepairing-pairable-host" is longer than the RFC 6763 default of 15;
    // mdns-sd caps the limit at 30.
    daemon
        .set_service_name_len_max(30)
        .map_err(|e| format!("mDNS-Namenslänge: {e}"))?;
    let short: String = ident.chars().filter(|c| c.is_ascii_alphanumeric()).take(8).collect();
    let svc = ServiceInfo::new(
        PAIRABLE_HOST_SERVICE_TYPE,
        &ident,
        &format!("ipas-{short}.local."),
        "",
        port,
        &props[..],
    )
    .map_err(|e| format!("mDNS-Dienst: {e}"))?
    .enable_addr_auto();
    let fullname = svc.get_fullname().to_string();
    daemon.register(svc).map_err(|e| format!("mDNS-Anmeldung: {e}"))?;
    emit(json!({"event": "advertising", "port": port,
                "message": format!("Warte auf das iPhone – dort „{name}“ zum Koppeln wählen")}));

    let result = async {
        let (stream, peer_addr) = tokio::time::timeout(Duration::from_secs(timeout), listener.accept())
            .await
            .map_err(|_| "Zeitüberschreitung – kein iPhone hat sich verbunden".to_string())?
            .map_err(|e| format!("accept: {e}"))?;
        progress(&format!("iPhone verbunden ({})", peer_addr.ip()));
        let mut host = PairableHost::new(RpPairingSocket::new_device(stream), info.clone());
        let peer = host
            .accept(&mut rpf, |pin| async move {
                emit(json!({"event": "show_pin", "pin": pin,
                            "message": "PIN am iPhone eingeben"}));
            })
            .await
            .map_err(|e| format!("Kopplung: {e:?}"))?;
        rpf.write_to_file(out).await.map_err(|e| format!("write {out}: {e}"))?;
        Ok::<Value, String>(json!({
            "udid": peer.remotepairing_udid,
            "name": peer.name,
            "model": peer.model,
            "ip": peer_addr.ip().to_string(),
            "identifier": rpf.identifier,
        }))
    }
    .await;

    let _ = daemon.unregister(&fullname);
    tokio::time::sleep(Duration::from_millis(300)).await;
    let _ = daemon.shutdown();
    result
}

fn cmd_discover(a: &Args) -> Res<Value> {
    use mdns_sd::{ServiceDaemon, ServiceEvent};
    let secs: u64 = a.get("timeout").and_then(|t| t.parse().ok()).unwrap_or(4);
    let daemon = ServiceDaemon::new().map_err(|e| format!("mDNS: {e}"))?;
    let types = ["_apple-mobdev2._tcp.local.", "_remotepairing._tcp.local."];
    let receivers: Vec<_> = types
        .iter()
        .filter_map(|t| daemon.browse(t).ok().map(|r| (*t, r)))
        .collect();
    let deadline = std::time::Instant::now() + Duration::from_secs(secs);
    let mut found: HashMap<String, Value> = HashMap::new();
    while std::time::Instant::now() < deadline {
        for (ty, rx) in &receivers {
            while let Ok(ev) = rx.recv_timeout(Duration::from_millis(100)) {
                if let ServiceEvent::ServiceResolved(s) = ev {
                    let mut v4: Vec<String> = s.get_addresses_v4().iter().map(|a| a.to_string()).collect();
                    v4.sort();
                    let all: Vec<String> = s.get_addresses().iter().map(|a| a.to_ip_addr().to_string()).collect();
                    let mut txt = serde_json::Map::new();
                    for k in ["identifier", "name", "model", "ver", "minVer"] {
                        if let Some(val) = s.get_property_val_str(k) {
                            txt.insert(k.into(), json!(val));
                        }
                    }
                    found.insert(
                        s.get_fullname().to_string(),
                        json!({
                            "type": ty.trim_end_matches(".local.").trim_start_matches('_').split('.').next().unwrap_or(""),
                            "fullname": s.get_fullname(),
                            "host": s.get_hostname(),
                            "port": s.get_port(),
                            "ipv4": v4,
                            "addresses": all,
                            "txt": txt,
                        }),
                    );
                }
            }
        }
    }
    let _ = daemon.shutdown();
    Ok(json!({"services": found.into_values().collect::<Vec<_>>()}))
}

#[tokio::main]
async fn main() {
    let args = match Args::parse() {
        Ok(a) => a,
        Err(e) => {
            emit(json!({"ok": false, "error": e}));
            std::process::exit(2);
        }
    };
    let result = match args.cmd.as_str() {
        "lockdown" => cmd_lockdown(&args).await,
        "rppair" => cmd_rppair(&args).await,
        "apps" => cmd_apps(&args).await,
        "push" => cmd_push(&args).await,
        "verify" => cmd_verify(&args).await,
        "rpverify" => cmd_rpverify(&args).await,
        "wifiaccept" => cmd_wifiaccept(&args).await,
        "discover" => tokio::task::spawn_blocking(move || cmd_discover(&args))
            .await
            .unwrap_or_else(|e| Err(e.to_string())),
        "version" => Ok(json!({"version": env!("CARGO_PKG_VERSION")})),
        other => Err(format!("unknown command {other}")),
    };
    match result {
        Ok(mut v) => {
            v["ok"] = json!(true);
            emit(v);
        }
        Err(e) => {
            emit(json!({"ok": false, "error": e}));
            std::process::exit(1);
        }
    }
}
