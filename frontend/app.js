const API_BASE = "http://localhost:80/api"; 
const WS_BASE = "ws://localhost:80/ws"; 

// Active State
let ws = null;
let jwtToken = null;
let myPlayerId = null;
let currentRoomId = null;
let isMyTurn = false;
let myPlayerIdx = -1; // Usually 1 or 2
let opponentId = null;
let reconnectAttempts = 0;
const MAX_RECONNECT = 5;
let seqNum = 1;

// DOM Selectors
const eStatusBadge = document.getElementById("status-badge");
const eAuthSec = document.getElementById("auth-section");
const eMmSec = document.getElementById("matchmaking-section");
const eGameSec = document.getElementById("game-section");
const eBoard = document.getElementById("game-board");

// Helpers
const logMessage = (msg, type = "info", data = null) => {
    const logContainer = document.getElementById("log-container");
    const entry = document.createElement("div");
    entry.className = `log-entry log-${type}`;
    let text = `[${new Date().toLocaleTimeString()}] ${msg}`;
    if (data) text += `<br><span style="opacity:0.6;font-size:0.9em">${JSON.stringify(data).substring(0, 100)}${JSON.stringify(data).length > 100 ? '...' : ''}</span>`;
    entry.innerHTML = text;
    logContainer.appendChild(entry);
    logContainer.scrollTop = logContainer.scrollHeight;
};

document.getElementById("btn-clear-logs").addEventListener("click", () => {
    document.getElementById("log-container").innerHTML = "";
});

// Authentication
document.getElementById("btn-register").addEventListener("click", () => authenticate(`${API_BASE}/register`, true));
document.getElementById("btn-login").addEventListener("click", () => authenticate(`${API_BASE}/login`, false));

async function authenticate(url, isRegister) {
    const user = document.getElementById("auth-username").value;
    const pass = document.getElementById("auth-password").value;
    if (!user || !pass) return document.getElementById("auth-error").textContent = "Username and password required";

    const payload = { username: user, password: pass };
    if (isRegister) payload.email = `${user}@example.com`;

    try {
        const res = await fetch(url, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify(payload)
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data.detail || "Authentication Failed");
        
        jwtToken = data.access_token;
        myPlayerId = data.player_id;
        
        document.getElementById("auth-error").classList.add("hidden");
        eAuthSec.classList.add("disabled");
        eMmSec.classList.remove("disabled");
        
        connectWebSocket();
    } catch (err) {
        document.getElementById("auth-error").textContent = err.message;
        document.getElementById("auth-error").classList.remove("hidden");
    }
}

// WebSocket connection
function connectWebSocket() {
    ws = new WebSocket(`${WS_BASE}?token=${jwtToken}`);
    
    ws.onopen = () => {
        reconnectAttempts = 0;
        eStatusBadge.className = "badge connected";
        eStatusBadge.textContent = "Server Online";
        document.getElementById("btn-find-match").disabled = false;
        logMessage("WS Attached to Game Node", "success");
    };
    
    ws.onclose = () => {
        eStatusBadge.className = "badge disconnected";
        eStatusBadge.textContent = "Offline";
        eGameSec.classList.add("disabled");
        if (reconnectAttempts < MAX_RECONNECT) {
            reconnectAttempts++;
            eStatusBadge.className = "badge reconnecting";
            eStatusBadge.textContent = "Reconnecting...";
            setTimeout(connectWebSocket, 3000);
        }
    };
    
    ws.onmessage = (event) => handleServerMessage(JSON.parse(event.data));
}

function sendWsMessage(type, payload = {}) {
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    ws.send(JSON.stringify({ type, ...payload }));
    logMessage(type, "send", payload);
}

// Matchmaking
document.getElementById("btn-find-match").addEventListener("click", () => {
    sendWsMessage("find_match");
    document.getElementById("btn-find-match").disabled = true;
    document.getElementById("btn-cancel-match").disabled = false;
    document.getElementById("mm-status").textContent = "Locating opponent...";
});

document.getElementById("btn-cancel-match").addEventListener("click", () => {
    sendWsMessage("cancel_match");
    document.getElementById("btn-find-match").disabled = false;
    document.getElementById("btn-cancel-match").disabled = true;
    document.getElementById("mm-status").textContent = "";
});

// Protocol handler
function handleServerMessage(msg) {
    logMessage(msg.type, "recv");
    switch (msg.type) {
        case "connected":
            document.getElementById("info-player-id").textContent = msg.player_id;
            break;
        case "matchmaking_queued":
            document.getElementById("mm-status").textContent = `Queued (Est Wait: ${msg.estimated_wait_sec}s)`;
            break;
        case "match_found":
            document.getElementById("info-room-id").textContent = msg.room_id;
            document.getElementById("mm-status").textContent = "Match found! Booting arena...";
            break;
        case "game_started":
            setupArena(msg);
            break;
        case "game_state_update":
            updateArena(msg);
            break;
        case "game_over":
            handleGameOver(msg);
            break;
    }
}

// Game Arena Logic
function setupArena(msg) {
    eMmSec.classList.add("disabled");
    eGameSec.classList.remove("disabled");
    
    const pIds = msg.initial_state.player_ids;
    myPlayerIdx = pIds.indexOf(myPlayerId) + 1; // 1 or 2
    
    // Set avatars
    document.getElementById("my-color-avatar").className = `avatar p${myPlayerIdx}`;
    document.getElementById("opp-color-avatar").className = `avatar p${myPlayerIdx === 1 ? 2 : 1}`;

    initBoard(10);
    updateArena(msg);
}

function updateArena(msg) {
    isMyTurn = msg.your_turn;
    const state = msg.state || msg.initial_state;
    
    // Scores
    const oppId = state.player_ids.find(id => id !== myPlayerId);
    document.getElementById("score-self").textContent = state.scores[myPlayerId] || 0;
    document.getElementById("score-opp").textContent = state.scores[oppId] || 0;
    
    // Turn Display
    const turnOrb = document.getElementById("turn-indicator");
    const turnTxt = document.getElementById("turn-text");
    if (isMyTurn) {
        turnOrb.className = "orb my-turn";
        turnOrb.textContent = "ACTION REQUIRED";
        turnTxt.textContent = "It is your turn to place territory.";
    } else {
        turnOrb.className = "orb opp-turn";
        turnOrb.textContent = "STANDBY";
        turnTxt.textContent = "Awaiting opponent action...";
    }

    // Sync board state visually
    const board = state.board;
    for (let y = 0; y < board.length; y++) {
        for (let x = 0; x < board[y].length; x++) {
            const cell = document.querySelector(`.grid-cell[data-x="${x}"][data-y="${y}"]`);
            if (board[y][x] !== 0) {
                cell.className = `grid-cell p${board[y][x]}-owned`;
            }
        }
    }
}

function handleGameOver(msg) {
    isMyTurn = false;
    const turnOrb = document.getElementById("turn-indicator");
    
    if (msg.winner_id === myPlayerId) {
        turnOrb.className = "orb my-turn";
        turnOrb.textContent = "VICTORY";
    } else if (!msg.winner_id || msg.result === 'draw') {
        turnOrb.className = "orb waiting";
        turnOrb.textContent = "DRAW";
    } else {
        turnOrb.className = "orb disconnected";
        turnOrb.textContent = "DEFEAT";
    }
    
    document.getElementById("turn-text").textContent = `ELO Delta: ${msg.elo_delta > 0 ? '+' : ''}${msg.elo_delta}`;
    
    setTimeout(() => {
        eMmSec.classList.remove("disabled");
        document.getElementById("btn-find-match").disabled = false;
        document.getElementById("mm-status").textContent = "Arena Closed. Re-queue available.";
    }, 3000);
}

// Board Grid
function initBoard(size) {
    eBoard.innerHTML = "";
    for (let y = 0; y < size; y++) {
        for (let x = 0; x < size; x++) {
            const cell = document.createElement("div");
            cell.className = "grid-cell";
            cell.dataset.x = x;
            cell.dataset.y = y;
            cell.addEventListener("click", () => {
                if (!isMyTurn || cell.classList.contains("p1-owned") || cell.classList.contains("p2-owned")) return;
                
                sendWsMessage("game_action", {
                    action: "place",
                    data: { x: x, y: y },
                    sequence_number: seqNum++
                });
            });
            eBoard.appendChild(cell);
        }
    }
}
