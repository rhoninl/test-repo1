```javascript
const http = require('http');
const { spawn } = require('child_process');
const url = require('url');

// Environment Variables
const CAMERA_IP = process.env.CAMERA_IP || '192.168.1.64';
const CAMERA_RTSP_PORT = process.env.CAMERA_RTSP_PORT || '554';
const CAMERA_USERNAME = process.env.CAMERA_USERNAME || 'admin';
const CAMERA_PASSWORD = process.env.CAMERA_PASSWORD || '12345';
const CAMERA_STREAM_PATH = process.env.CAMERA_STREAM_PATH || 'Streaming/Channels/101';
const SERVER_HOST = process.env.SERVER_HOST || '0.0.0.0';
const SERVER_PORT = parseInt(process.env.SERVER_PORT || '8080', 10);

let ffmpegProcess = null;
let clients = [];
let streaming = false;

// Generate RTSP URL
function getRtspUrl() {
  return `rtsp://${CAMERA_USERNAME}:${CAMERA_PASSWORD}@${CAMERA_IP}:${CAMERA_RTSP_PORT}/${CAMERA_STREAM_PATH}`;
}

function startStream(res) {
  if (streaming) {
    res.writeHead(200, {'Content-Type': 'application/json'});
    res.end(JSON.stringify({status: 'already streaming'}));
    return;
  }
  streaming = true;
  res.writeHead(200, {'Content-Type': 'application/json'});
  res.end(JSON.stringify({status: 'stream started'}));
}

function stopStream(res) {
  if (!streaming) {
    res.writeHead(200, {'Content-Type': 'application/json'});
    res.end(JSON.stringify({status: 'not streaming'}));
    return;
  }
  streaming = false;
  if (ffmpegProcess) {
    ffmpegProcess.kill('SIGKILL');
    ffmpegProcess = null;
  }
  clients.forEach(c => {
    try { c.end(); } catch (e) {}
  });
  clients = [];
  res.writeHead(200, {'Content-Type': 'application/json'});
  res.end(JSON.stringify({status: 'stream stopped'}));
}

// Start ffmpeg process and pipe to clients
function startFfmpeg() {
  if (ffmpegProcess) return;

  const args = [
    '-rtsp_transport', 'tcp',
    '-i', getRtspUrl(),
    '-f', 'mp4',
    '-movflags', 'frag_keyframe+empty_moov+default_base_moof',
    '-an',
    '-vf', 'scale=iw:ih',
    '-vcodec', 'copy',
    '-reset_timestamps', '1',
    'pipe:1'
  ];

  ffmpegProcess = spawn('ffmpeg', args);

  ffmpegProcess.stdout.on('data', chunk => {
    clients.forEach(res => {
      try {
        res.write(chunk);
      } catch (e) { /* ignore */ }
    });
  });

  ffmpegProcess.stderr.on('data', () => { /* ignore ffmpeg logs */ });

  ffmpegProcess.on('close', () => {
    ffmpegProcess = null;
    clients.forEach(res => {
      try { res.end(); } catch (e) {}
    });
    clients = [];
    streaming = false;
  });
}

function serveStream(req, res) {
  if (!streaming) {
    res.writeHead(503, {'Content-Type': 'application/json'});
    res.end(JSON.stringify({error: 'stream not started'}));
    return;
  }
  res.writeHead(200, {
    'Content-Type': 'video/mp4',
    'Connection': 'close',
    'Accept-Ranges': 'bytes',
    'Cache-Control': 'no-cache'
  });
  clients.push(res);

  if (!ffmpegProcess) startFfmpeg();

  req.on('close', () => {
    clients = clients.filter(c => c !== res);
    if (clients.length === 0 && ffmpegProcess) {
      ffmpegProcess.kill('SIGKILL');
      ffmpegProcess = null;
      streaming = false;
    }
  });
}

const server = http.createServer((req, res) => {
  const parsedUrl = url.parse(req.url, true);
  if (req.method === 'POST' && parsedUrl.pathname === '/stream/start') {
    startStream(res);
  } else if (req.method === 'POST' && parsedUrl.pathname === '/stream/stop') {
    stopStream(res);
  } else if (req.method === 'GET' && parsedUrl.pathname === '/stream/live') {
    serveStream(req, res);
  } else {
    res.writeHead(404, {'Content-Type': 'application/json'});
    res.end(JSON.stringify({error: 'Not found'}));
  }
});

server.listen(SERVER_PORT, SERVER_HOST, () => {});

process.on('SIGINT', () => {
  if (ffmpegProcess) ffmpegProcess.kill('SIGKILL');
  process.exit();
});
```
