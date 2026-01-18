#!/usr/bin/env python3
"""Podcast Player - Self-contained podcast feed reader and player."""

import sqlite3
import json
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from urllib.request import urlopen, Request
from urllib.error import URLError
import re
import html

from flask import Flask, request, jsonify, Response

app = Flask(__name__)
DB_PATH = Path("/data/podcasts.db") if Path("/data").exists() else Path("podcasts.db")


def get_db():
    """Get database connection."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Initialize database schema."""
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS feeds (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            url TEXT UNIQUE NOT NULL,
            title TEXT NOT NULL,
            description TEXT,
            image_url TEXT,
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS episodes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            feed_id INTEGER NOT NULL,
            guid TEXT,
            title TEXT NOT NULL,
            description TEXT,
            audio_url TEXT NOT NULL,
            pub_date TEXT,
            duration TEXT,
            played BOOLEAN DEFAULT 0,
            position REAL DEFAULT 0,
            FOREIGN KEY (feed_id) REFERENCES feeds(id) ON DELETE CASCADE,
            UNIQUE(feed_id, audio_url)
        );

        CREATE INDEX IF NOT EXISTS idx_episodes_feed ON episodes(feed_id);
    """)
    conn.commit()
    conn.close()


def parse_duration(duration_str):
    """Parse various duration formats to human-readable string."""
    if not duration_str:
        return None

    duration_str = duration_str.strip()

    # Already in HH:MM:SS or MM:SS format
    if ":" in duration_str:
        return duration_str

    # Seconds as integer
    try:
        seconds = int(duration_str)
        h, remainder = divmod(seconds, 3600)
        m, s = divmod(remainder, 60)
        if h > 0:
            return f"{h}:{m:02d}:{s:02d}"
        return f"{m}:{s:02d}"
    except ValueError:
        return duration_str


def parse_date(date_str):
    """Parse RSS date to ISO format."""
    if not date_str:
        return None

    # Common RSS date formats
    formats = [
        "%a, %d %b %Y %H:%M:%S %z",
        "%a, %d %b %Y %H:%M:%S %Z",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%d",
    ]

    for fmt in formats:
        try:
            dt = datetime.strptime(date_str.strip(), fmt)
            return dt.strftime("%Y-%m-%d %H:%M")
        except ValueError:
            continue

    return date_str[:25] if len(date_str) > 25 else date_str


def fetch_feed(url):
    """Fetch and parse RSS feed."""
    headers = {
        "User-Agent": "PodcastPlayer/1.0",
        "Accept": "application/rss+xml, application/xml, text/xml, */*"
    }

    req = Request(url, headers=headers)

    try:
        with urlopen(req, timeout=30) as response:
            content = response.read()
    except URLError as e:
        raise Exception(f"Failed to fetch feed: {e}")

    # Parse XML
    try:
        root = ET.fromstring(content)
    except ET.ParseError as e:
        raise Exception(f"Invalid RSS feed: {e}")

    # Handle different RSS formats
    channel = root.find("channel")
    if channel is None:
        channel = root.find(".//{http://www.w3.org/2005/Atom}feed")
        if channel is None:
            raise Exception("No channel found in feed")

    # Namespace handling for iTunes tags
    ns = {
        "itunes": "http://www.itunes.com/dtds/podcast-1.0.dtd",
        "content": "http://purl.org/rss/1.0/modules/content/",
        "atom": "http://www.w3.org/2005/Atom"
    }

    def find_text(elem, paths):
        for path in paths:
            el = elem.find(path, ns) if ":" in path else elem.find(path)
            if el is not None and el.text:
                return el.text.strip()
        return None

    # Extract feed info
    title = find_text(channel, ["title"]) or "Unknown Podcast"
    description = find_text(channel, ["description", "itunes:summary"])

    # Get feed image
    image_url = None
    itunes_image = channel.find("itunes:image", ns)
    if itunes_image is not None:
        image_url = itunes_image.get("href")
    if not image_url:
        image_el = channel.find("image/url")
        if image_el is not None:
            image_url = image_el.text

    # Extract episodes
    episodes = []
    for item in channel.findall("item"):
        enclosure = item.find("enclosure")
        audio_url = enclosure.get("url") if enclosure is not None else None

        if not audio_url:
            # Try media:content
            media = item.find(".//{http://search.yahoo.com/mrss/}content")
            if media is not None:
                audio_url = media.get("url")

        if not audio_url:
            continue

        ep_title = find_text(item, ["title"]) or "Untitled"
        ep_description = find_text(item, ["description", "content:encoded", "itunes:summary"])
        pub_date = find_text(item, ["pubDate", "published"])
        duration = find_text(item, ["itunes:duration", "duration"])
        guid = find_text(item, ["guid"]) or audio_url

        # Clean description (remove HTML tags for preview)
        if ep_description:
            ep_description = html.unescape(re.sub(r'<[^>]+>', '', ep_description))[:500]

        episodes.append({
            "guid": guid,
            "title": ep_title,
            "description": ep_description,
            "audio_url": audio_url,
            "pub_date": parse_date(pub_date),
            "duration": parse_duration(duration)
        })

    return {
        "title": title,
        "description": description,
        "image_url": image_url,
        "episodes": episodes
    }


@app.route("/")
def index():
    """Serve the main application."""
    return Response(HTML_TEMPLATE, mimetype="text/html")


@app.route("/api/feeds", methods=["GET"])
def list_feeds():
    """List all feeds."""
    conn = get_db()
    feeds = conn.execute(
        "SELECT id, url, title, description, image_url FROM feeds ORDER BY added_at DESC"
    ).fetchall()
    conn.close()
    return jsonify([dict(f) for f in feeds])


@app.route("/api/feeds", methods=["POST"])
def add_feed():
    """Add a new feed."""
    data = request.get_json()
    url = data.get("url", "").strip()

    if not url:
        return jsonify({"error": "URL is required"}), 400

    # Check if already exists
    conn = get_db()
    existing = conn.execute("SELECT id FROM feeds WHERE url = ?", (url,)).fetchone()
    if existing:
        conn.close()
        return jsonify({"error": "Feed already exists"}), 409

    # Fetch and parse feed
    try:
        feed_data = fetch_feed(url)
    except Exception as e:
        conn.close()
        return jsonify({"error": str(e)}), 400

    # Insert feed
    cursor = conn.execute(
        "INSERT INTO feeds (url, title, description, image_url) VALUES (?, ?, ?, ?)",
        (url, feed_data["title"], feed_data["description"], feed_data["image_url"])
    )
    feed_id = cursor.lastrowid

    # Insert episodes
    for ep in feed_data["episodes"]:
        conn.execute("""
            INSERT OR IGNORE INTO episodes
            (feed_id, guid, title, description, audio_url, pub_date, duration)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (feed_id, ep["guid"], ep["title"], ep["description"],
              ep["audio_url"], ep["pub_date"], ep["duration"]))

    conn.commit()
    conn.close()

    return jsonify({
        "id": feed_id,
        "url": url,
        "title": feed_data["title"],
        "description": feed_data["description"],
        "image_url": feed_data["image_url"],
        "episode_count": len(feed_data["episodes"])
    }), 201


@app.route("/api/feeds/<int:feed_id>", methods=["DELETE"])
def delete_feed(feed_id):
    """Delete a feed."""
    conn = get_db()
    conn.execute("DELETE FROM feeds WHERE id = ?", (feed_id,))
    conn.commit()
    conn.close()
    return "", 204


@app.route("/api/feeds/<int:feed_id>/refresh", methods=["POST"])
def refresh_feed(feed_id):
    """Refresh episodes for a feed."""
    conn = get_db()
    feed = conn.execute("SELECT url FROM feeds WHERE id = ?", (feed_id,)).fetchone()

    if not feed:
        conn.close()
        return jsonify({"error": "Feed not found"}), 404

    try:
        feed_data = fetch_feed(feed["url"])
    except Exception as e:
        conn.close()
        return jsonify({"error": str(e)}), 400

    # Update feed info
    conn.execute(
        "UPDATE feeds SET title = ?, description = ?, image_url = ? WHERE id = ?",
        (feed_data["title"], feed_data["description"], feed_data["image_url"], feed_id)
    )

    # Insert new episodes
    new_count = 0
    for ep in feed_data["episodes"]:
        cursor = conn.execute("""
            INSERT OR IGNORE INTO episodes
            (feed_id, guid, title, description, audio_url, pub_date, duration)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (feed_id, ep["guid"], ep["title"], ep["description"],
              ep["audio_url"], ep["pub_date"], ep["duration"]))
        if cursor.rowcount > 0:
            new_count += 1

    conn.commit()
    conn.close()

    return jsonify({"new_episodes": new_count})


@app.route("/api/feeds/<int:feed_id>/episodes", methods=["GET"])
def list_episodes(feed_id):
    """List episodes for a feed."""
    conn = get_db()
    episodes = conn.execute("""
        SELECT id, title, description, audio_url, pub_date, duration, played, position
        FROM episodes WHERE feed_id = ?
        ORDER BY pub_date DESC, id DESC
    """, (feed_id,)).fetchall()
    conn.close()
    return jsonify([dict(e) for e in episodes])


@app.route("/api/episodes/<int:episode_id>/progress", methods=["PUT"])
def update_progress(episode_id):
    """Update playback progress for an episode."""
    data = request.get_json()
    position = data.get("position", 0)
    played = data.get("played", False)

    conn = get_db()
    conn.execute(
        "UPDATE episodes SET position = ?, played = ? WHERE id = ?",
        (position, played, episode_id)
    )
    conn.commit()
    conn.close()
    return "", 204


@app.route("/api/episodes/<int:episode_id>/played", methods=["PUT"])
def mark_played(episode_id):
    """Toggle played status."""
    conn = get_db()
    conn.execute(
        "UPDATE episodes SET played = NOT played WHERE id = ?",
        (episode_id,)
    )
    conn.commit()
    conn.close()
    return "", 204


HTML_TEMPLATE = '''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Podcast Player</title>
    <style>
        :root {
            --bg-primary: #0f0f1a;
            --bg-secondary: #1a1a2e;
            --bg-tertiary: #252542;
            --accent: #e94560;
            --accent-hover: #ff6b6b;
            --text-primary: #ffffff;
            --text-secondary: #a0a0b0;
            --text-muted: #606070;
            --border: #303050;
            --success: #4ade80;
        }

        * { box-sizing: border-box; margin: 0; padding: 0; }

        body {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            background: var(--bg-primary);
            color: var(--text-primary);
            min-height: 100vh;
            display: flex;
        }

        /* Sidebar */
        .sidebar {
            width: 280px;
            background: var(--bg-secondary);
            border-right: 1px solid var(--border);
            display: flex;
            flex-direction: column;
            height: 100vh;
            position: fixed;
            left: 0;
            top: 0;
        }

        .sidebar-header {
            padding: 20px;
            border-bottom: 1px solid var(--border);
        }

        .sidebar-header h1 {
            font-size: 20px;
            font-weight: 600;
        }

        .add-feed-form {
            padding: 15px 20px;
            border-bottom: 1px solid var(--border);
        }

        .add-feed-form input {
            width: 100%;
            padding: 10px 12px;
            border: 1px solid var(--border);
            border-radius: 6px;
            background: var(--bg-tertiary);
            color: var(--text-primary);
            font-size: 13px;
            margin-bottom: 8px;
        }

        .add-feed-form input::placeholder { color: var(--text-muted); }
        .add-feed-form input:focus { outline: none; border-color: var(--accent); }

        .add-feed-form button {
            width: 100%;
            padding: 10px;
            background: var(--accent);
            color: white;
            border: none;
            border-radius: 6px;
            font-size: 13px;
            font-weight: 500;
            cursor: pointer;
            transition: background 0.2s;
        }

        .add-feed-form button:hover { background: var(--accent-hover); }
        .add-feed-form button:disabled { opacity: 0.5; cursor: not-allowed; }

        .feed-list {
            flex: 1;
            overflow-y: auto;
            padding: 10px;
        }

        .feed-item {
            display: flex;
            align-items: center;
            gap: 12px;
            padding: 12px;
            border-radius: 8px;
            cursor: pointer;
            transition: background 0.2s;
            margin-bottom: 4px;
        }

        .feed-item:hover { background: var(--bg-tertiary); }
        .feed-item.active { background: var(--bg-tertiary); border-left: 3px solid var(--accent); }

        .feed-image {
            width: 44px;
            height: 44px;
            border-radius: 6px;
            background: var(--bg-tertiary);
            object-fit: cover;
            flex-shrink: 0;
        }

        .feed-image.placeholder {
            display: flex;
            align-items: center;
            justify-content: center;
            font-size: 20px;
        }

        .feed-info { flex: 1; min-width: 0; }
        .feed-info h3 {
            font-size: 14px;
            font-weight: 500;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }

        .feed-actions {
            opacity: 0;
            transition: opacity 0.2s;
        }

        .feed-item:hover .feed-actions { opacity: 1; }

        .feed-actions button {
            background: none;
            border: none;
            color: var(--text-muted);
            cursor: pointer;
            padding: 4px;
            font-size: 16px;
        }

        .feed-actions button:hover { color: var(--accent); }

        /* Main Content */
        .main-content {
            margin-left: 280px;
            flex: 1;
            display: flex;
            flex-direction: column;
            min-height: 100vh;
            padding-bottom: 100px;
        }

        .content-header {
            padding: 30px;
            background: linear-gradient(135deg, var(--bg-secondary) 0%, var(--bg-tertiary) 100%);
            border-bottom: 1px solid var(--border);
        }

        .content-header h2 {
            font-size: 28px;
            font-weight: 600;
            margin-bottom: 8px;
        }

        .content-header p {
            color: var(--text-secondary);
            font-size: 14px;
            line-height: 1.5;
            max-width: 600px;
        }

        .content-actions {
            margin-top: 15px;
            display: flex;
            gap: 10px;
        }

        .content-actions button {
            padding: 8px 16px;
            border-radius: 6px;
            font-size: 13px;
            cursor: pointer;
            transition: all 0.2s;
        }

        .btn-primary {
            background: var(--accent);
            color: white;
            border: none;
        }

        .btn-primary:hover { background: var(--accent-hover); }

        .btn-secondary {
            background: transparent;
            color: var(--text-secondary);
            border: 1px solid var(--border);
        }

        .btn-secondary:hover { background: var(--bg-tertiary); color: var(--text-primary); }

        .episode-list { padding: 20px 30px; }

        .episode-item {
            display: flex;
            align-items: center;
            gap: 15px;
            padding: 15px;
            background: var(--bg-secondary);
            border-radius: 10px;
            margin-bottom: 10px;
            cursor: pointer;
            transition: all 0.2s;
            border: 2px solid transparent;
        }

        .episode-item:hover { background: var(--bg-tertiary); }
        .episode-item.playing { border-color: var(--accent); }
        .episode-item.played { opacity: 0.6; }

        .episode-play-btn {
            width: 40px;
            height: 40px;
            border-radius: 50%;
            background: var(--accent);
            border: none;
            color: white;
            font-size: 14px;
            cursor: pointer;
            display: flex;
            align-items: center;
            justify-content: center;
            flex-shrink: 0;
            transition: transform 0.2s;
        }

        .episode-play-btn:hover { transform: scale(1.1); }

        .episode-info { flex: 1; min-width: 0; }

        .episode-title {
            font-size: 15px;
            font-weight: 500;
            margin-bottom: 4px;
            display: flex;
            align-items: center;
            gap: 8px;
        }

        .episode-title .played-badge {
            font-size: 10px;
            padding: 2px 6px;
            background: var(--success);
            color: var(--bg-primary);
            border-radius: 3px;
            font-weight: 600;
        }

        .episode-meta {
            font-size: 13px;
            color: var(--text-muted);
            display: flex;
            gap: 12px;
        }

        .episode-description {
            font-size: 13px;
            color: var(--text-secondary);
            margin-top: 6px;
            line-height: 1.4;
            display: -webkit-box;
            -webkit-line-clamp: 2;
            -webkit-box-orient: vertical;
            overflow: hidden;
        }

        .episode-progress {
            width: 100px;
            height: 4px;
            background: var(--bg-tertiary);
            border-radius: 2px;
            overflow: hidden;
            margin-top: 8px;
        }

        .episode-progress-bar {
            height: 100%;
            background: var(--accent);
            border-radius: 2px;
        }

        /* Player */
        .player {
            position: fixed;
            bottom: 0;
            left: 280px;
            right: 0;
            background: var(--bg-secondary);
            border-top: 1px solid var(--border);
            padding: 15px 30px;
            display: none;
            align-items: center;
            gap: 20px;
        }

        .player.visible { display: flex; }

        .player-info { flex: 1; min-width: 0; }

        .player-title {
            font-size: 14px;
            font-weight: 500;
            white-space: nowrap;
            overflow: hidden;
            text-overflow: ellipsis;
        }

        .player-podcast {
            font-size: 12px;
            color: var(--text-muted);
        }

        .player-controls {
            display: flex;
            align-items: center;
            gap: 15px;
        }

        .player-controls button {
            background: none;
            border: none;
            color: var(--text-primary);
            cursor: pointer;
            font-size: 18px;
            padding: 8px;
            border-radius: 50%;
            transition: background 0.2s;
        }

        .player-controls button:hover { background: var(--bg-tertiary); }

        .player-controls .play-pause {
            width: 44px;
            height: 44px;
            background: var(--accent);
            border-radius: 50%;
            display: flex;
            align-items: center;
            justify-content: center;
        }

        .player-controls .play-pause:hover { background: var(--accent-hover); }

        .player-progress {
            flex: 2;
            display: flex;
            align-items: center;
            gap: 10px;
        }

        .player-progress input[type="range"] {
            flex: 1;
            height: 4px;
            -webkit-appearance: none;
            background: var(--bg-tertiary);
            border-radius: 2px;
            cursor: pointer;
        }

        .player-progress input[type="range"]::-webkit-slider-thumb {
            -webkit-appearance: none;
            width: 12px;
            height: 12px;
            background: var(--accent);
            border-radius: 50%;
        }

        .player-time {
            font-size: 12px;
            color: var(--text-muted);
            min-width: 45px;
        }

        .player-volume {
            display: flex;
            align-items: center;
            gap: 8px;
        }

        .player-volume input[type="range"] {
            width: 80px;
            height: 4px;
            -webkit-appearance: none;
            background: var(--bg-tertiary);
            border-radius: 2px;
        }

        .player-volume input[type="range"]::-webkit-slider-thumb {
            -webkit-appearance: none;
            width: 10px;
            height: 10px;
            background: var(--text-secondary);
            border-radius: 50%;
        }

        /* Empty state */
        .empty-state {
            text-align: center;
            padding: 80px 20px;
            color: var(--text-muted);
        }

        .empty-state h3 {
            font-size: 18px;
            margin-bottom: 8px;
            color: var(--text-secondary);
        }

        /* Toast */
        .toast {
            position: fixed;
            bottom: 120px;
            right: 30px;
            background: var(--bg-tertiary);
            padding: 12px 20px;
            border-radius: 8px;
            font-size: 14px;
            opacity: 0;
            transform: translateY(10px);
            transition: all 0.3s;
            z-index: 1000;
        }

        .toast.visible { opacity: 1; transform: translateY(0); }
        .toast.error { background: #3d1f2a; color: #ff6b6b; }

        /* Loading */
        .loading {
            display: flex;
            align-items: center;
            justify-content: center;
            padding: 40px;
            color: var(--text-muted);
        }

        @keyframes spin {
            to { transform: rotate(360deg); }
        }

        .spinner {
            width: 24px;
            height: 24px;
            border: 2px solid var(--border);
            border-top-color: var(--accent);
            border-radius: 50%;
            animation: spin 0.8s linear infinite;
            margin-right: 10px;
        }

        /* Responsive */
        @media (max-width: 768px) {
            .sidebar { width: 100%; height: auto; position: relative; }
            .main-content { margin-left: 0; }
            .player { left: 0; flex-wrap: wrap; padding: 10px 15px; }
            .player-progress { order: 3; width: 100%; flex: auto; }
        }
    </style>
</head>
<body>
    <aside class="sidebar">
        <div class="sidebar-header">
            <h1>Podcasts</h1>
        </div>
        <div class="add-feed-form">
            <input type="text" id="feedUrl" placeholder="Paste RSS feed URL...">
            <button id="addFeedBtn">Add Podcast</button>
        </div>
        <div class="feed-list" id="feedList"></div>
    </aside>

    <main class="main-content">
        <div id="welcomeState" class="empty-state">
            <h3>Welcome to Podcast Player</h3>
            <p>Add a podcast feed to get started</p>
        </div>

        <div id="feedContent" style="display: none;">
            <div class="content-header" id="contentHeader">
                <h2 id="feedTitle">Podcast Title</h2>
                <p id="feedDescription"></p>
                <div class="content-actions">
                    <button class="btn-primary" id="refreshBtn">Refresh</button>
                    <button class="btn-secondary" id="deleteBtn">Remove</button>
                </div>
            </div>
            <div class="episode-list" id="episodeList"></div>
        </div>

        <div id="loadingState" class="loading" style="display: none;">
            <div class="spinner"></div>
            Loading...
        </div>
    </main>

    <div class="player" id="player">
        <div class="player-info">
            <div class="player-title" id="playerTitle">-</div>
            <div class="player-podcast" id="playerPodcast">-</div>
        </div>
        <div class="player-controls">
            <button id="skipBack" title="Back 15s">⏪</button>
            <button class="play-pause" id="playPause">▶</button>
            <button id="skipForward" title="Forward 30s">⏩</button>
        </div>
        <div class="player-progress">
            <span class="player-time" id="currentTime">0:00</span>
            <input type="range" id="progressBar" min="0" max="100" value="0">
            <span class="player-time" id="duration">0:00</span>
        </div>
        <div class="player-volume">
            <span>🔊</span>
            <input type="range" id="volumeBar" min="0" max="100" value="100">
        </div>
    </div>

    <div class="toast" id="toast"></div>

    <audio id="audio"></audio>

    <script>
        const API = "/api";
        let feeds = [];
        let currentFeed = null;
        let currentEpisode = null;
        let episodes = [];

        const audio = document.getElementById("audio");
        const player = document.getElementById("player");
        const playPauseBtn = document.getElementById("playPause");
        const progressBar = document.getElementById("progressBar");
        const volumeBar = document.getElementById("volumeBar");
        const currentTimeEl = document.getElementById("currentTime");
        const durationEl = document.getElementById("duration");
        const playerTitle = document.getElementById("playerTitle");
        const playerPodcast = document.getElementById("playerPodcast");

        // Toast notifications
        function showToast(message, isError = false) {
            const toast = document.getElementById("toast");
            toast.textContent = message;
            toast.className = "toast visible" + (isError ? " error" : "");
            setTimeout(() => toast.className = "toast", 3000);
        }

        // Format time
        function formatTime(seconds) {
            if (!seconds || isNaN(seconds)) return "0:00";
            const h = Math.floor(seconds / 3600);
            const m = Math.floor((seconds % 3600) / 60);
            const s = Math.floor(seconds % 60);
            if (h > 0) return `${h}:${m.toString().padStart(2, "0")}:${s.toString().padStart(2, "0")}`;
            return `${m}:${s.toString().padStart(2, "0")}`;
        }

        // API helpers
        async function api(path, options = {}) {
            const res = await fetch(API + path, {
                headers: { "Content-Type": "application/json" },
                ...options,
                body: options.body ? JSON.stringify(options.body) : undefined
            });
            if (!res.ok && res.status !== 204) {
                const data = await res.json().catch(() => ({}));
                throw new Error(data.error || "Request failed");
            }
            if (res.status === 204) return null;
            return res.json();
        }

        // Load feeds
        async function loadFeeds() {
            feeds = await api("/feeds");
            renderFeeds();
        }

        // Render feed list
        function renderFeeds() {
            const list = document.getElementById("feedList");
            list.innerHTML = feeds.map(f => `
                <div class="feed-item ${currentFeed?.id === f.id ? 'active' : ''}" data-id="${f.id}">
                    ${f.image_url
                        ? `<img class="feed-image" src="${f.image_url}" alt="">`
                        : `<div class="feed-image placeholder">🎙️</div>`}
                    <div class="feed-info">
                        <h3>${escapeHtml(f.title)}</h3>
                    </div>
                    <div class="feed-actions">
                        <button class="delete-feed" title="Remove">×</button>
                    </div>
                </div>
            `).join("");

            document.getElementById("welcomeState").style.display = feeds.length ? "none" : "block";

            // Click handlers
            list.querySelectorAll(".feed-item").forEach(el => {
                el.addEventListener("click", (e) => {
                    if (!e.target.classList.contains("delete-feed")) {
                        selectFeed(parseInt(el.dataset.id));
                    }
                });
            });

            list.querySelectorAll(".delete-feed").forEach(btn => {
                btn.addEventListener("click", async (e) => {
                    e.stopPropagation();
                    const id = parseInt(btn.closest(".feed-item").dataset.id);
                    await api(`/feeds/${id}`, { method: "DELETE" });
                    if (currentFeed?.id === id) {
                        currentFeed = null;
                        document.getElementById("feedContent").style.display = "none";
                        document.getElementById("welcomeState").style.display = "block";
                    }
                    await loadFeeds();
                    showToast("Feed removed");
                });
            });
        }

        // Select feed
        async function selectFeed(id) {
            const feed = feeds.find(f => f.id === id);
            if (!feed) return;

            currentFeed = feed;
            renderFeeds();

            document.getElementById("welcomeState").style.display = "none";
            document.getElementById("feedContent").style.display = "none";
            document.getElementById("loadingState").style.display = "flex";

            try {
                episodes = await api(`/feeds/${id}/episodes`);
                renderFeedContent();
            } catch (e) {
                showToast(e.message, true);
            } finally {
                document.getElementById("loadingState").style.display = "none";
            }
        }

        // Render feed content
        function renderFeedContent() {
            document.getElementById("feedContent").style.display = "block";
            document.getElementById("feedTitle").textContent = currentFeed.title;
            document.getElementById("feedDescription").textContent = currentFeed.description || "";

            const list = document.getElementById("episodeList");
            list.innerHTML = episodes.map(ep => `
                <div class="episode-item ${currentEpisode?.id === ep.id ? 'playing' : ''} ${ep.played ? 'played' : ''}" data-id="${ep.id}">
                    <button class="episode-play-btn">${currentEpisode?.id === ep.id && !audio.paused ? '⏸' : '▶'}</button>
                    <div class="episode-info">
                        <div class="episode-title">
                            ${escapeHtml(ep.title)}
                            ${ep.played ? '<span class="played-badge">PLAYED</span>' : ''}
                        </div>
                        <div class="episode-meta">
                            ${ep.pub_date ? `<span>${ep.pub_date}</span>` : ''}
                            ${ep.duration ? `<span>${ep.duration}</span>` : ''}
                        </div>
                        ${ep.description ? `<div class="episode-description">${escapeHtml(ep.description)}</div>` : ''}
                        ${ep.position > 0 && !ep.played ? `
                            <div class="episode-progress">
                                <div class="episode-progress-bar" style="width: ${Math.min(100, ep.position / 36)}%"></div>
                            </div>
                        ` : ''}
                    </div>
                </div>
            `).join("");

            list.querySelectorAll(".episode-item").forEach(el => {
                el.addEventListener("click", () => {
                    const ep = episodes.find(e => e.id === parseInt(el.dataset.id));
                    if (ep) playEpisode(ep);
                });
            });
        }

        // Play episode
        function playEpisode(episode) {
            const wasPlaying = currentEpisode?.id === episode.id;

            if (wasPlaying) {
                if (audio.paused) audio.play();
                else audio.pause();
                return;
            }

            currentEpisode = episode;
            audio.src = episode.audio_url;
            audio.currentTime = episode.position || 0;
            audio.play();

            playerTitle.textContent = episode.title;
            playerPodcast.textContent = currentFeed.title;
            player.classList.add("visible");

            renderFeedContent();
        }

        // Audio event handlers
        audio.addEventListener("play", () => {
            playPauseBtn.textContent = "⏸";
            renderFeedContent();
        });

        audio.addEventListener("pause", () => {
            playPauseBtn.textContent = "▶";
            saveProgress();
            renderFeedContent();
        });

        audio.addEventListener("timeupdate", () => {
            if (!audio.duration) return;
            const pct = (audio.currentTime / audio.duration) * 100;
            progressBar.value = pct;
            currentTimeEl.textContent = formatTime(audio.currentTime);
        });

        audio.addEventListener("loadedmetadata", () => {
            durationEl.textContent = formatTime(audio.duration);
        });

        audio.addEventListener("ended", () => {
            if (currentEpisode) {
                api(`/episodes/${currentEpisode.id}/progress`, {
                    method: "PUT",
                    body: { position: 0, played: true }
                });
                currentEpisode.played = true;
                currentEpisode.position = 0;
                renderFeedContent();
            }
        });

        // Save progress periodically
        let lastSave = 0;
        audio.addEventListener("timeupdate", () => {
            const now = Date.now();
            if (now - lastSave > 10000 && currentEpisode) {
                lastSave = now;
                saveProgress();
            }
        });

        async function saveProgress() {
            if (!currentEpisode) return;
            currentEpisode.position = audio.currentTime;
            await api(`/episodes/${currentEpisode.id}/progress`, {
                method: "PUT",
                body: { position: audio.currentTime, played: false }
            });
        }

        // Player controls
        playPauseBtn.addEventListener("click", () => {
            if (audio.paused) audio.play();
            else audio.pause();
        });

        document.getElementById("skipBack").addEventListener("click", () => {
            audio.currentTime = Math.max(0, audio.currentTime - 15);
        });

        document.getElementById("skipForward").addEventListener("click", () => {
            audio.currentTime = Math.min(audio.duration, audio.currentTime + 30);
        });

        progressBar.addEventListener("input", () => {
            if (audio.duration) {
                audio.currentTime = (progressBar.value / 100) * audio.duration;
            }
        });

        volumeBar.addEventListener("input", () => {
            audio.volume = volumeBar.value / 100;
        });

        // Add feed
        document.getElementById("addFeedBtn").addEventListener("click", addFeed);
        document.getElementById("feedUrl").addEventListener("keypress", (e) => {
            if (e.key === "Enter") addFeed();
        });

        async function addFeed() {
            const input = document.getElementById("feedUrl");
            const url = input.value.trim();
            if (!url) return;

            const btn = document.getElementById("addFeedBtn");
            btn.disabled = true;
            btn.textContent = "Adding...";

            try {
                const feed = await api("/feeds", { method: "POST", body: { url } });
                input.value = "";
                await loadFeeds();
                selectFeed(feed.id);
                showToast(`Added "${feed.title}"`);
            } catch (e) {
                showToast(e.message, true);
            } finally {
                btn.disabled = false;
                btn.textContent = "Add Podcast";
            }
        }

        // Refresh feed
        document.getElementById("refreshBtn").addEventListener("click", async () => {
            if (!currentFeed) return;
            const btn = document.getElementById("refreshBtn");
            btn.disabled = true;
            btn.textContent = "Refreshing...";

            try {
                const result = await api(`/feeds/${currentFeed.id}/refresh`, { method: "POST" });
                episodes = await api(`/feeds/${currentFeed.id}/episodes`);
                renderFeedContent();
                showToast(result.new_episodes ? `Found ${result.new_episodes} new episode(s)` : "No new episodes");
            } catch (e) {
                showToast(e.message, true);
            } finally {
                btn.disabled = false;
                btn.textContent = "Refresh";
            }
        });

        // Delete feed
        document.getElementById("deleteBtn").addEventListener("click", async () => {
            if (!currentFeed || !confirm("Remove this podcast?")) return;
            await api(`/feeds/${currentFeed.id}`, { method: "DELETE" });
            currentFeed = null;
            document.getElementById("feedContent").style.display = "none";
            document.getElementById("welcomeState").style.display = "block";
            await loadFeeds();
            showToast("Feed removed");
        });

        // Escape HTML
        function escapeHtml(text) {
            if (!text) return "";
            const div = document.createElement("div");
            div.textContent = text;
            return div.innerHTML;
        }

        // Initialize
        loadFeeds();
    </script>
</body>
</html>
'''

if __name__ == "__main__":
    init_db()
    app.run(host="0.0.0.0", port=8080)
