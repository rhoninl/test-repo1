#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <signal.h>
#include <errno.h>
#include <sys/types.h>
#include <sys/socket.h>
#include <sys/wait.h>
#include <netinet/in.h>
#include <arpa/inet.h>
#include <pthread.h>
#include <fcntl.h>
#include <sys/select.h>
#include <sys/time.h>
#include <time.h>
#include <ctype.h>
#include <netdb.h>

// ============ CONFIGURATION FROM ENV =============

#define ENVSTRLEN 256

static char DEVICE_IP[ENVSTRLEN] = {0};
static char DEVICE_USER[ENVSTRLEN] = {0};
static char DEVICE_PASS[ENVSTRLEN] = {0};
static char RTSP_PATH[ENVSTRLEN]  = {0};
static char HTTP_HOST[ENVSTRLEN]  = {0};
static int  HTTP_PORT = 0;
static int  SNAPSHOT_PORT = 0;
static int  RTSP_PORT = 0;

// ============ HELPER MACROS ======================

#define HTTP_RESP_HEADER_200_IMAGE \
    "HTTP/1.1 200 OK\r\n" \
    "Content-Type: image/jpeg\r\n" \
    "Cache-Control: no-cache\r\n" \
    "Connection: close\r\n" \
    "\r\n"

#define HTTP_RESP_HEADER_200_STREAM \
    "HTTP/1.1 200 OK\r\n" \
    "Content-Type: multipart/x-mixed-replace; boundary=frame\r\n" \
    "Cache-Control: no-cache\r\n" \
    "Connection: close\r\n" \
    "\r\n"

#define HTTP_RESP_HEADER_400 \
    "HTTP/1.1 400 Bad Request\r\n" \
    "Content-Type: text/plain\r\n" \
    "Connection: close\r\n" \
    "\r\nBad Request"

#define HTTP_RESP_HEADER_404 \
    "HTTP/1.1 404 Not Found\r\n" \
    "Content-Type: text/plain\r\n" \
    "Connection: close\r\n" \
    "\r\nNot Found"

#define HTTP_RESP_HEADER_405 \
    "HTTP/1.1 405 Method Not Allowed\r\n" \
    "Content-Type: text/plain\r\n" \
    "Connection: close\r\n" \
    "\r\nMethod Not Allowed"

#define HTTP_RESP_HEADER_500 \
    "HTTP/1.1 500 Internal Server Error\r\n" \
    "Content-Type: text/plain\r\n" \
    "Connection: close\r\n" \
    "\r\nInternal Server Error"

// ============ BASIC BASE64 =======================

static const char b64tab[] = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

void base64_encode(const char* src, char* dst) {
    int i = 0, j = 0, len = strlen(src);
    unsigned char a3[3];
    unsigned char a4[4];
    while (len--) {
        a3[i++] = *(src++);
        if (i == 3) {
            a4[0] = (a3[0] & 0xfc) >> 2;
            a4[1] = ((a3[0] & 0x03) << 4) + ((a3[1] & 0xf0) >> 4);
            a4[2] = ((a3[1] & 0x0f) << 2) + ((a3[2] & 0xc0) >> 6);
            a4[3] = a3[2] & 0x3f;
            for (i = 0; i < 4; i++)
                *dst++ = b64tab[a4[i]];
            i = 0;
        }
    }
    if (i) {
        for (int k = i; k < 3; k++)
            a3[k] = '\0';
        a4[0] = (a3[0] & 0xfc) >> 2;
        a4[1] = ((a3[0] & 0x03) << 4) + ((a3[1] & 0xf0) >> 4);
        a4[2] = ((a3[1] & 0x0f) << 2) + ((a3[2] & 0xc0) >> 6);
        a4[3] = a3[2] & 0x3f;
        for (int k = 0; k < i + 1; k++)
            *dst++ = b64tab[a4[k]];
        while (i++ < 3)
            *dst++ = '=';
    }
    *dst = '\0';
}

// ============ SIMPLE HTTP CLIENT ==================

int http_get_snapshot(char* ip, int port, char* user, char* pass, int sock) {
    char req[512];
    char auth[ENVSTRLEN*2];
    char b64[ENVSTRLEN*2] = {0};
    int ret;

    if (user[0] != '\0') {
        snprintf(auth, sizeof(auth), "%s:%s", user, pass);
        base64_encode(auth, b64);
        snprintf(req, sizeof(req),
            "GET /ISAPI/Streaming/channels/101/picture HTTP/1.1\r\n"
            "Host: %s:%d\r\n"
            "Authorization: Basic %s\r\n"
            "Connection: close\r\n"
            "\r\n", ip, port, b64);
    } else {
        snprintf(req, sizeof(req),
            "GET /ISAPI/Streaming/channels/101/picture HTTP/1.1\r\n"
            "Host: %s:%d\r\n"
            "Connection: close\r\n"
            "\r\n", ip, port);
    }

    int cam_sock = socket(AF_INET, SOCK_STREAM, 0);
    if (cam_sock < 0) return -1;
    struct sockaddr_in cam_addr;
    cam_addr.sin_family = AF_INET;
    cam_addr.sin_port = htons(port);
    inet_pton(AF_INET, ip, &cam_addr.sin_addr);
    if (connect(cam_sock, (struct sockaddr*)&cam_addr, sizeof(cam_addr)) < 0) {
        close(cam_sock);
        return -1;
    }
    send(cam_sock, req, strlen(req), 0);

    // Read HTTP header and look for start of JPEG
    char buf[4096];
    int jpg_start = 0, jpg_end = 0, total = 0;
    int hdr_end = 0, n, found = 0;
    char* p;
    while ((n = recv(cam_sock, buf, sizeof(buf), 0)) > 0) {
        if (!hdr_end) {
            p = strstr(buf, "\r\n\r\n");
            if (p) {
                int pre = p - buf + 4;
                write(sock, HTTP_RESP_HEADER_200_IMAGE, strlen(HTTP_RESP_HEADER_200_IMAGE));
                write(sock, buf+pre, n-pre);
                hdr_end = 1;
            }
        } else {
            write(sock, buf, n);
        }
    }
    close(cam_sock);
    return 0;
}

// =============== RTSP TO MJPEG ====================

// This is a minimalistic MJPEG-over-HTTP proxy for the /feed endpoint.
// It receives raw H.264 RTSP stream, decodes I-frames, and re-encodes as JPEG on-the-fly.
// For simplicity, below uses open-source libraries in real-world code, but here we stick to raw C and only extract JPEGs from Hikvision's RTSP (since many send periodic JPEGs in RTP).
// This is a basic implementation and works only for Hikvision/ISAPI cameras with JPEG RTP payload support ("JPEG over RTP").

static int connect_rtsp(char* ip, int port, char* user, char* pass, char* stream_path, int* rtp_sock, char* session_id, char* out_url) {
    // Using TCP interleaved RTP (port reuse via RTSP TCP control channel)
    int sock = socket(AF_INET, SOCK_STREAM, 0);
    if (sock < 0) return -1;
    struct sockaddr_in addr;
    addr.sin_family = AF_INET;
    addr.sin_port = htons(port);
    inet_pton(AF_INET, ip, &addr.sin_addr);
    if (connect(sock, (struct sockaddr*)&addr, sizeof(addr)) < 0) {
        close(sock);
        return -1;
    }
    char b64[ENVSTRLEN*2] = {0};
    char auth[ENVSTRLEN*2] = {0};
    char req[512], res[2048], *p;
    int n, cseq = 1;

    // RTSP DESCRIBE
    if (user[0] != '\0') {
        snprintf(auth, sizeof(auth), "%s:%s", user, pass);
        base64_encode(auth, b64);
        snprintf(req, sizeof(req),
            "DESCRIBE rtsp://%s:%d/%s RTSP/1.0\r\n"
            "CSeq: %d\r\n"
            "Accept: application/sdp\r\n"
            "Authorization: Basic %s\r\n"
            "\r\n", ip, port, stream_path, cseq++, b64);
    } else {
        snprintf(req, sizeof(req),
            "DESCRIBE rtsp://%s:%d/%s RTSP/1.0\r\n"
            "CSeq: %d\r\n"
            "Accept: application/sdp\r\n"
            "\r\n", ip, port, stream_path, cseq++);
    }
    send(sock, req, strlen(req), 0);
    n = recv(sock, res, sizeof(res)-1, 0); res[n] = 0;
    if (strstr(res, "401 Unauthorized")) { close(sock); return -2; }
    if (!strstr(res, "200 OK")) { close(sock); return -1; }

    // RTSP SETUP (interleaved)
    int ch = 0;
    if (user[0] != '\0') {
        snprintf(req, sizeof(req),
            "SETUP rtsp://%s:%d/%s/trackID=1 RTSP/1.0\r\n"
            "CSeq: %d\r\n"
            "Transport: RTP/AVP/TCP;unicast;interleaved=%d-%d\r\n"
            "Authorization: Basic %s\r\n"
            "\r\n", ip, port, stream_path, cseq++, ch, ch+1, b64);
    } else {
        snprintf(req, sizeof(req),
            "SETUP rtsp://%s:%d/%s/trackID=1 RTSP/1.0\r\n"
            "CSeq: %d\r\n"
            "Transport: RTP/AVP/TCP;unicast;interleaved=%d-%d\r\n"
            "\r\n", ip, port, stream_path, cseq++, ch, ch+1);
    }
    send(sock, req, strlen(req), 0);
    n = recv(sock, res, sizeof(res)-1, 0); res[n] = 0;
    if (!strstr(res, "200 OK")) { close(sock); return -1; }
    p = strstr(res, "Session: ");
    if (p) {
        sscanf(p, "Session: %s", session_id);
        char* semi = strchr(session_id, ';');
        if (semi) *semi = 0;
    }

    // RTSP PLAY
    if (user[0] != '\0') {
        snprintf(req, sizeof(req),
            "PLAY rtsp://%s:%d/%s RTSP/1.0\r\n"
            "CSeq: %d\r\n"
            "Session: %s\r\n"
            "Authorization: Basic %s\r\n"
            "\r\n", ip, port, stream_path, cseq++, session_id, b64);
    } else {
        snprintf(req, sizeof(req),
            "PLAY rtsp://%s:%d/%s RTSP/1.0\r\n"
            "CSeq: %d\r\n"
            "Session: %s\r\n"
            "\r\n", ip, port, stream_path, cseq++, session_id);
    }
    send(sock, req, strlen(req), 0);
    n = recv(sock, res, sizeof(res)-1, 0); res[n] = 0;
    if (!strstr(res, "200 OK")) { close(sock); return -1; }
    *rtp_sock = sock;
    if (out_url)
        snprintf(out_url, ENVSTRLEN*2, "rtsp://%s:%d/%s", ip, port, stream_path);
    return 0;
}

// RTP JPEG payload parsing for MJPEG over HTTP
void* mjpeg_proxy_thread(void* arg) {
    int client = *(int*)arg;
    free(arg);
    int rtp_sock, ret;
    char session_id[128] = {0};
    char out_url[ENVSTRLEN*2] = {0};

    ret = connect_rtsp(DEVICE_IP, RTSP_PORT, DEVICE_USER, DEVICE_PASS, RTSP_PATH, &rtp_sock, session_id, out_url);
    if (ret != 0) {
        write(client, HTTP_RESP_HEADER_500, strlen(HTTP_RESP_HEADER_500));
        close(client);
        return NULL;
    }

    write(client, HTTP_RESP_HEADER_200_STREAM, strlen(HTTP_RESP_HEADER_200_STREAM));
    fd_set fds;
    struct timeval tv;
    char buf[4096];
    unsigned char jpeg[1024*64];
    size_t jpglen = 0;

    int running = 1;
    while (running) {
        FD_ZERO(&fds);
        FD_SET(rtp_sock, &fds);
        tv.tv_sec = 2;
        tv.tv_usec = 0;
        int sel = select(rtp_sock+1, &fds, NULL, NULL, &tv);
        if (sel > 0 && FD_ISSET(rtp_sock, &fds)) {
            int n = recv(rtp_sock, buf, sizeof(buf), 0);
            if (n <= 0) break;
            // Parse RTP interleaved header
            int off = 0;
            while (off + 4 < n) {
                if (buf[off] != '$') break;
                int ch = buf[off+1];
                int plen = (unsigned char)buf[off+2] << 8 | (unsigned char)buf[off+3];
                if (off+4+plen > n) break;
                if ((buf[off+4] & 0x7F) == 26) {
                    // JPEG RTP payload
                    // For simplicity, just forward the JPEG bytestream as MJPEG frame
                    // Find JPEG SOI
                    for (int i=off+12; i<off+4+plen-1; ++i) {
                        if ((unsigned char)buf[i]==0xFF && (unsigned char)buf[i+1]==0xD8) {
                            // Find EOI
                            for (int j=i+2; j<off+4+plen-1; ++j) {
                                if ((unsigned char)buf[j]==0xFF && (unsigned char)buf[j+1]==0xD9) {
                                    size_t len = j+2-i;
                                    char mhead[256];
                                    snprintf(mhead, sizeof(mhead),
                                        "--frame\r\n"
                                        "Content-Type: image/jpeg\r\n"
                                        "Content-Length: %zu\r\n\r\n", len);
                                    write(client, mhead, strlen(mhead));
                                    write(client, buf+i, len);
                                    write(client, "\r\n", 2);
                                    break;
                                }
                            }
                            break;
                        }
                    }
                }
                off += 4+plen;
            }
        }
    }
    close(rtp_sock);
    close(client);
    return NULL;
}

// =============== RECORD CONTROL ===================

int isapi_record_control(const char* action) {
    char req[512];
    char auth[ENVSTRLEN*2];
    char b64[ENVSTRLEN*2] = {0};
    int port = SNAPSHOT_PORT;
    if (port == 0) port = 80;
    if (DEVICE_USER[0] != '\0') {
        snprintf(auth, sizeof(auth), "%s:%s", DEVICE_USER, DEVICE_PASS);
        base64_encode(auth, b64);
        snprintf(req, sizeof(req),
            "PUT /ISAPI/ContentMgmt/record/control HTTP/1.1\r\n"
            "Host: %s:%d\r\n"
            "Authorization: Basic %s\r\n"
            "Content-Type: application/xml\r\n"
            "Content-Length: %d\r\n"
            "Connection: close\r\n"
            "\r\n"
            "<ContentMgmtRecordCtrl version=\"2.0\" xmlns=\"http://www.isapi.org/ver20/XMLSchema\">\n"
            "<command>%s</command>\n"
            "</ContentMgmtRecordCtrl>\n",
            DEVICE_IP, port, b64, 147+(int)strlen(action), action);
    } else {
        snprintf(req, sizeof(req),
            "PUT /ISAPI/ContentMgmt/record/control HTTP/1.1\r\n"
            "Host: %s:%d\r\n"
            "Content-Type: application/xml\r\n"
            "Content-Length: %d\r\n"
            "Connection: close\r\n"
            "\r\n"
            "<ContentMgmtRecordCtrl version=\"2.0\" xmlns=\"http://www.isapi.org/ver20/XMLSchema\">\n"
            "<command>%s</command>\n"
            "</ContentMgmtRecordCtrl>\n",
            DEVICE_IP, port, 147+(int)strlen(action), action);
    }

    int cam_sock = socket(AF_INET, SOCK_STREAM, 0);
    if (cam_sock < 0) return -1;
    struct sockaddr_in cam_addr;
    cam_addr.sin_family = AF_INET;
    cam_addr.sin_port = htons(port);
    inet_pton(AF_INET, DEVICE_IP, &cam_addr.sin_addr);
    if (connect(cam_sock, (struct sockaddr*)&cam_addr, sizeof(cam_addr)) < 0) {
        close(cam_sock);
        return -1;
    }
    send(cam_sock, req, strlen(req), 0);

    char buf[4096];
    int n = recv(cam_sock, buf, sizeof(buf), 0);
    close(cam_sock);
    if (n < 0) return -1;
    if (strstr(buf, "200 OK")) return 0;
    return -1;
}

// =============== DEVICE INFO ======================

void send_info(int client) {
    char info[2048];
    snprintf(info, sizeof(info),
        "HTTP/1.1 200 OK\r\n"
        "Content-Type: application/json\r\n"
        "Connection: close\r\n"
        "\r\n"
        "{\n"
        "  \"device_name\": \"IP Surveillance Device (RaCM)\",\n"
        "  \"device_model\": \"DS-2CD876MF, DS-2CD877MF, DS-2CD855MF, MCV-100F, MCV-250PTZ, MCV-500, MaxView Series, AXIS210a, ISAPI-compliant generic devices\",\n"
        "  \"manufacturer\": \"HIKVISION, ClearView, GB28181, ONVIF, SONY\",\n"
        "  \"device_type\": \"DVR, NVR, IP Camera, Video Recorder\",\n"
        "  \"protocols\": [\"ISAPI (HTTP/REST/XML)\", \"RTSP\"],\n"
        "  \"status\": \"operational\"\n"
        "}\n"
    );
    write(client, info, strlen(info));
}

// =============== HTTP SERVER ======================

void* client_thread(void* arg) {
    int client = *(int*)arg;
    free(arg);
    char reqline[1024] = {0};
    char buf[4096] = {0};
    int n = recv(client, buf, sizeof(buf)-1, 0);
    if (n <= 0) { close(client); return NULL; }
    buf[n] = 0;
    sscanf(buf, "%1023[^\r\n]", reqline);
    char method[8], path[128];
    sscanf(reqline, "%7s %127s", method, path);

    if (strcmp(method, "GET") == 0 && strcmp(path, "/info") == 0) {
        send_info(client);
        close(client);
        return NULL;
    }
    if (strcmp(method, "GET") == 0 && strcmp(path, "/snap") == 0) {
        if (http_get_snapshot(DEVICE_IP, SNAPSHOT_PORT, DEVICE_USER, DEVICE_PASS, client) != 0) {
            write(client, HTTP_RESP_HEADER_500, strlen(HTTP_RESP_HEADER_500));
        }
        close(client);
        return NULL;
    }
    if (strcmp(method, "GET") == 0 && strcmp(path, "/feed") == 0) {
        int* pc = malloc(sizeof(int));
        *pc = client;
        pthread_t tid;
        pthread_create(&tid, NULL, mjpeg_proxy_thread, pc);
        pthread_detach(tid);
        return NULL;
    }
    if (strcmp(method, "POST") == 0 && strcmp(path, "/record/start") == 0) {
        if (isapi_record_control("start") == 0)
            write(client, "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n{\"result\":\"ok\"}\n", 65);
        else
            write(client, HTTP_RESP_HEADER_500, strlen(HTTP_RESP_HEADER_500));
        close(client);
        return NULL;
    }
    if (strcmp(method, "POST") == 0 && strcmp(path, "/record/stop") == 0) {
        if (isapi_record_control("stop") == 0)
            write(client, "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n{\"result\":\"ok\"}\n", 65);
        else
            write(client, HTTP_RESP_HEADER_500, strlen(HTTP_RESP_HEADER_500));
        close(client);
        return NULL;
    }
    // Method not allowed or unknown endpoint
    write(client, HTTP_RESP_HEADER_404, strlen(HTTP_RESP_HEADER_404));
    close(client);
    return NULL;
}

void load_env() {
    char* p;
    p = getenv("DEVICE_IP"); if (p) strncpy(DEVICE_IP, p, ENVSTRLEN-1);
    p = getenv("DEVICE_USER"); if (p) strncpy(DEVICE_USER, p, ENVSTRLEN-1);
    p = getenv("DEVICE_PASS"); if (p) strncpy(DEVICE_PASS, p, ENVSTRLEN-1);
    p = getenv("RTSP_PATH"); if (p) strncpy(RTSP_PATH, p, ENVSTRLEN-1); else strcpy(RTSP_PATH, "Streaming/Channels/101");
    p = getenv("HTTP_HOST"); if (p) strncpy(HTTP_HOST, p, ENVSTRLEN-1); else strcpy(HTTP_HOST, "0.0.0.0");
    p = getenv("HTTP_PORT"); if (p) HTTP_PORT = atoi(p); else HTTP_PORT = 8080;
    p = getenv("RTSP_PORT"); if (p) RTSP_PORT = atoi(p); else RTSP_PORT = 554;
    p = getenv("SNAPSHOT_PORT"); if (p) SNAPSHOT_PORT = atoi(p); else SNAPSHOT_PORT = 80;
}

int main() {
    load_env();

    int srv = socket(AF_INET, SOCK_STREAM, 0);
    if (srv < 0) { perror("socket"); exit(1); }
    int opt=1; setsockopt(srv, SOL_SOCKET, SO_REUSEADDR, &opt, sizeof(opt));
    struct sockaddr_in addr;
    addr.sin_family = AF_INET;
    addr.sin_addr.s_addr = inet_addr(HTTP_HOST);
    addr.sin_port = htons(HTTP_PORT);
    if (bind(srv, (struct sockaddr*)&addr, sizeof(addr)) < 0) {
        perror("bind"); exit(1);
    }
    listen(srv, 16);

    printf("HTTP server started on %s:%d\n", HTTP_HOST, HTTP_PORT);
    while (1) {
        struct sockaddr_in cli_addr;
        socklen_t cli_len = sizeof(cli_addr);
        int cli = accept(srv, (struct sockaddr*)&cli_addr, &cli_len);
        if (cli < 0) continue;
        int* pcli = malloc(sizeof(int));
        *pcli = cli;
        pthread_t tid;
        pthread_create(&tid, NULL, client_thread, pcli);
        pthread_detach(tid);
    }
    close(srv);
    return 0;
}