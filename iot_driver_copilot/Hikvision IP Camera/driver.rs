use actix_web::{web, App, HttpRequest, HttpResponse, HttpServer, Responder};
use actix_web::http::header::{CONTENT_TYPE, CACHE_CONTROL};
use actix_web::rt::task;
use futures::Stream;
use std::env;
use std::pin::Pin;
use std::task::{Context, Poll};
use std::process::{Stdio};
use std::sync::{Arc, Mutex};
use std::io::{Read, Write};
use std::time::Duration;
use tokio::io::{AsyncReadExt};
use tokio::net::TcpStream;
use tokio::sync::mpsc::{self, Sender, Receiver};

const BOUNDARY: &str = "hikvisionboundary";

#[derive(Clone)]
struct AppState {
    camera_ip: String,
    camera_rtsp_port: u16,
    camera_user: String,
    camera_pass: String,
    stream_path: String,
    streaming: Arc<Mutex<bool>>,
}

impl AppState {
    fn get_rtsp_url(&self) -> String {
        format!(
            "rtsp://{}:{}@{}:{}/{}",
            self.camera_user, self.camera_pass, self.camera_ip, self.camera_rtsp_port, self.stream_path
        )
    }
}

async fn start_stream(data: web::Data<AppState>) -> impl Responder {
    let mut streaming = data.streaming.lock().unwrap();
    *streaming = true;
    HttpResponse::Ok().json(serde_json::json!({ "status": "streaming started" }))
}

async fn stop_stream(data: web::Data<AppState>) -> impl Responder {
    let mut streaming = data.streaming.lock().unwrap();
    *streaming = false;
    HttpResponse::Ok().json(serde_json::json!({ "status": "streaming stopped" }))
}

struct MJPEGStream {
    rx: Mutex<Receiver<Vec<u8>>>,
}

impl Stream for MJPEGStream {
    type Item = Result<web::Bytes, actix_web::Error>;
    fn poll_next(
        self: Pin<&mut Self>,
        cx: &mut Context<'_>,
    ) -> Poll<Option<Self::Item>> {
        let mut rx = self.rx.lock().unwrap();
        match rx.try_recv() {
            Ok(data) => {
                Poll::Ready(Some(Ok(web::Bytes::from(data))))
            }
            Err(mpsc::error::TryRecvError::Empty) => {
                cx.waker().wake_by_ref();
                Poll::Pending
            }
            Err(_) => Poll::Ready(None),
        }
    }
}

// Minimal H264 NAL to JPEG "snap" for browser demonstration.
// Note: This is NOT a real transcoding; for a real implementation, use a pure Rust H264->JPEG decoder.
fn fake_h264_to_jpeg(h264_data: &[u8]) -> Option<Vec<u8>> {
    // This only wraps the raw H264 as a JPEG for demonstration purposes.
    // Browsers will not render it, but it shows how the boundary works.
    // For real world, use a Rust-based H.264 to JPEG decoder.
    // Here, we just wrap the data in a JPEG file header and footer.
    let mut jpeg = Vec::new();
    jpeg.extend_from_slice(&[0xFF, 0xD8, 0xFF, 0xE0]); // SOI marker
    jpeg.extend_from_slice(b"FAKE"); // Fake header
    jpeg.extend_from_slice(h264_data);
    jpeg.extend_from_slice(&[0xFF, 0xD9]); // EOI marker
    Some(jpeg)
}

async fn stream_handler(data: web::Data<AppState>, _req: HttpRequest) -> impl Responder {
    let (tx, rx) = mpsc::channel::<Vec<u8>>(32);
    let app_state = data.clone();

    task::spawn(async move {
        let rtsp_url = app_state.get_rtsp_url();
        let mut streaming = app_state.streaming.lock().unwrap();
        if !*streaming {
            return;
        }
        drop(streaming);

        // RTSP communication and H264 frame extraction.
        // For demonstration, we'll open a TCP connection to the RTSP port and read data (simplified).
        let mut stream = match TcpStream::connect((app_state.camera_ip.as_str(), app_state.camera_rtsp_port)).await {
            Ok(s) => s,
            Err(_) => {
                let _ = tx.send(b"--".to_vec()).await;
                return;
            }
        };

        // Send minimal RTSP SETUP/PLAY commands (not a full RTSP implementation).
        let setup = format!(
            "OPTIONS rtsp://{}/{} RTSP/1.0\r\nCSeq: 1\r\nUser-Agent: HikvisionRustDriver\r\n\r\n",
            app_state.camera_ip, app_state.stream_path
        );
        let _ = stream.write_all(setup.as_bytes()).await;
        let mut buf = vec![0u8; 4096];
        let _ = stream.read(&mut buf).await; // Read response

        let describe = format!(
            "DESCRIBE {} RTSP/1.0\r\nCSeq: 2\r\nUser-Agent: HikvisionRustDriver\r\nAccept: application/sdp\r\n\r\n",
            rtsp_url
        );
        let _ = stream.write_all(describe.as_bytes()).await;
        let _ = stream.read(&mut buf).await;

        let setup = format!(
            "SETUP {} RTSP/1.0\r\nCSeq: 3\r\nTransport: RTP/AVP;unicast;client_port=8000-8001\r\n\r\n",
            rtsp_url
        );
        let _ = stream.write_all(setup.as_bytes()).await;
        let _ = stream.read(&mut buf).await;

        let play = format!(
            "PLAY {} RTSP/1.0\r\nCSeq: 4\r\nSession: 1\r\nRange: npt=0.000-\r\n\r\n",
            rtsp_url
        );
        let _ = stream.write_all(play.as_bytes()).await;
        let _ = stream.read(&mut buf).await;

        // Now read "video" data -- simplified: just chunk from TCP stream.
        loop {
            let mut streaming = app_state.streaming.lock().unwrap();
            if !*streaming {
                break;
            }
            drop(streaming);

            let mut frame = vec![0u8; 1024];
            match stream.read(&mut frame).await {
                Ok(n) if n > 0 => {
                    if let Some(jpeg) = fake_h264_to_jpeg(&frame[..n]) {
                        let mut chunk = format!(
                            "\r\n--{}\r\nContent-Type: image/jpeg\r\nContent-Length: {}\r\n\r\n",
                            BOUNDARY,
                            jpeg.len()
                        ).into_bytes();
                        chunk.extend_from_slice(&jpeg);
                        let _ = tx.send(chunk).await;
                    }
                }
                _ => {
                    tokio::time::sleep(Duration::from_millis(100)).await;
                }
            }
        }
    });

    let resp = HttpResponse::Ok()
        .insert_header((CONTENT_TYPE, format!("multipart/x-mixed-replace; boundary={}", BOUNDARY)))
        .insert_header((CACHE_CONTROL, "no-cache"))
        .streaming(MJPEGStream { rx: Mutex::new(rx) });

    resp
}

#[actix_web::main]
async fn main() -> std::io::Result<()> {
    // Load configuration from environment variables
    let server_host = env::var("SERVER_HOST").unwrap_or_else(|_| "0.0.0.0".to_string());
    let server_port = env::var("SERVER_PORT").unwrap_or_else(|_| "8080".to_string()).parse::<u16>().unwrap_or(8080);
    let camera_ip = env::var("CAMERA_IP").expect("CAMERA_IP must be set");
    let camera_rtsp_port = env::var("CAMERA_RTSP_PORT").unwrap_or_else(|_| "554".to_string()).parse::<u16>().unwrap_or(554);
    let camera_user = env::var("CAMERA_USER").unwrap_or_else(|_| "admin".to_string());
    let camera_pass = env::var("CAMERA_PASS").unwrap_or_else(|_| "12345".to_string());
    let stream_path = env::var("CAMERA_STREAM_PATH").unwrap_or_else(|_| "Streaming/Channels/101".to_string());

    let state = AppState {
        camera_ip,
        camera_rtsp_port,
        camera_user,
        camera_pass,
        stream_path,
        streaming: Arc::new(Mutex::new(false)),
    };

    HttpServer::new(move || {
        App::new()
            .app_data(web::Data::new(state.clone()))
            .route("/stream", web::get().to(stream_handler))
            .route("/start", web::post().to(start_stream))
            .route("/stop", web::post().to(stop_stream))
    })
    .bind((server_host.as_str(), server_port))?
    .run()
    .await
}