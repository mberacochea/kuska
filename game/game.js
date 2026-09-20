// Game UI controller - connects engine and AI to the DOM

import {
  EMPTY,
  LINES,
  createBoard,
  currentPlayer,
  legalMoves,
  applyMove,
  winner,
  isDraw,
  status
} from "./engine.js";

import {
  chooseMove
} from "./ai.js";

// Module-level game state
let board = createBoard();
let gameMode = "two-player"; // "two-player" or "vs-computer"
let difficulty = "hard"; // "easy", "medium", or "hard"
let isThinking = false; // Block input while computer is thinking
let focusedIndex = 0; // Track which cell has keyboard focus
let gameWasFinished = false; // Track if the game was finished to update score exactly once

// Scoreboard state
let scores = {
  x: 0,
  o: 0,
  draw: 0
};

const STORAGE_KEY = "achka.scores";

/**
 * Validate scores object to ensure it has the right shape and valid numbers.
 * Returns true if valid, false otherwise.
 */
function isValidScores(obj) {
  if (!obj || typeof obj !== "object") return false;
  const hasX = typeof obj.x === "number" && obj.x >= 0 && obj.x === Math.floor(obj.x);
  const hasO = typeof obj.o === "number" && obj.o >= 0 && obj.o === Math.floor(obj.o);
  const hasDraw = typeof obj.draw === "number" && obj.draw >= 0 && obj.draw === Math.floor(obj.draw);
  return hasX && hasO && hasDraw;
}

/**
 * Load scores from localStorage with validation and try/catch fallback.
 * If localStorage is unavailable or data is corrupt, use in-memory defaults.
 */
function loadScores() {
  try {
    const stored = localStorage.getItem(STORAGE_KEY);
    if (stored) {
      const parsed = JSON.parse(stored);
      if (isValidScores(parsed)) {
        scores = parsed;
        return;
      }
    }
  } catch (err) {
    // localStorage is unavailable (private mode, sandboxed, etc.)
    // Fall back to in-memory scores
  }
  // Reset to defaults if not loaded or invalid
  scores = { x: 0, o: 0, draw: 0 };
}

/**
 * Save scores to localStorage with try/catch fallback.
 * If localStorage is unavailable, continue with in-memory scores.
 */
function saveScores() {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(scores));
  } catch (err) {
    // localStorage is unavailable - continue with in-memory scores
  }
}

/**
 * Update the scoreboard display elements.
 */
function renderScoreboard() {
  document.getElementById("score-x").textContent = scores.x;
  document.getElementById("score-o").textContent = scores.o;
  document.getElementById("score-draw").textContent = scores.draw;
}

/**
 * Update scores based on game result and save to localStorage.
 * Only call when a game has just finished.
 */
function updateScoresFromGameEnd() {
  const gameStatus = status(board);
  if (gameStatus.state === "won") {
    if (gameStatus.player === "X") {
      scores.x++;
    } else {
      scores.o++;
    }
  } else if (gameStatus.state === "draw") {
    scores.draw++;
  }
  saveScores();
  renderScoreboard();
}

/**
 * Render the board state to the DOM.
 * - Paint marks (X/O) in cells
 * - Disable occupied cells and all cells when game is over
 * - Add .win class to cells in the winning line
 * - Update #status with turn info or game result
 * - Show/hide difficulty select based on game mode
 * - Disable input while computer is thinking
 * - Add aria-labels for accessibility
 * - Add animation class for new marks
 */
function render() {
  const boardEl = document.getElementById("board");
  const statusEl = document.getElementById("status");
  const difficultyEl = document.getElementById("difficulty");
  const gameStatus = status(board);

  // Determine if game is over
  const isGameOver = gameStatus.state === "won" || gameStatus.state === "draw";

  // Update scores if game just finished (exactly once)
  if (isGameOver && !gameWasFinished) {
    gameWasFinished = true;
    updateScoresFromGameEnd();
  }

  // Get winning line indices if any
  const winningLine = gameStatus.line || [];
  const winningSet = new Set(winningLine);

  // Update each cell
  boardEl.querySelectorAll(".cell").forEach((cell, index) => {
    const mark = board[index];
    const row = Math.floor(index / 3) + 1;
    const col = (index % 3) + 1;

    // Set the mark text
    cell.textContent = mark;

    // Add aria-label for accessibility
    let ariaLabel = `row ${row}, column ${col}`;
    if (mark !== EMPTY) {
      ariaLabel += `, ${mark}`;
    } else {
      ariaLabel += ", empty";
    }
    cell.setAttribute("aria-label", ariaLabel);

    // Disable if occupied, game is over, or computer is thinking
    const isOccupied = mark !== EMPTY;
    cell.disabled = isOccupied || isGameOver || isThinking;

    // Add/remove win class and animation
    if (winningSet.has(index)) {
      cell.classList.add("win");
    } else {
      cell.classList.remove("win");
    }

    // Add mark animation class (but only once - when mark first appears)
    if (mark !== EMPTY && !cell.dataset.marked) {
      cell.classList.add("mark");
      cell.dataset.marked = "true";
    }
  });

  // Show/hide and enable/disable difficulty select
  if (gameMode === "two-player") {
    difficultyEl.disabled = true;
  } else {
    difficultyEl.disabled = false;
  }

  // Update status text
  if (gameStatus.state === "won") {
    statusEl.textContent = `${gameStatus.player} wins!`;
  } else if (gameStatus.state === "draw") {
    statusEl.textContent = "Draw";
  } else {
    if (gameMode === "vs-computer" && gameStatus.player === "O") {
      statusEl.textContent = "Computer is thinking...";
    } else {
      statusEl.textContent = `${gameStatus.player}'s turn`;
    }
  }
}

/**
 * Play a move at the given index.
 * Apply the move if legal, otherwise do nothing silently.
 * In vs-computer mode, trigger computer's move after human move.
 */
function playMove(index) {
  // Don't allow moves while computer is thinking
  if (isThinking) return;

  // Silently ignore if cell is occupied or game is over
  try {
    board = applyMove(board, index);
    render();

    // If in vs-computer mode and game is not over, make computer move
    if (gameMode === "vs-computer") {
      const gameStatus = status(board);
      if (gameStatus.state === "playing") {
        makeComputerMove();
      }
    }
  } catch (err) {
    // Illegal move - do nothing, no console noise
  }
}

/**
 * Handle a cell click using event delegation.
 * Apply the move if legal, otherwise do nothing silently.
 */
function handleCellClick(e) {
  const cell = e.target.closest(".cell");
  if (!cell) return;

  const index = parseInt(cell.dataset.index, 10);
  playMove(index);
}

/**
 * Handle keyboard navigation and Enter/Space to play.
 * Arrow keys move focus across the 3x3 grid with wrap-around.
 * Enter or Space plays the focused cell.
 */
function handleKeyDown(e) {
  const cells = Array.from(document.querySelectorAll(".cell"));

  if (e.key === "ArrowUp") {
    e.preventDefault();
    focusedIndex = focusedIndex < 3 ? focusedIndex + 6 : focusedIndex - 3;
    cells[focusedIndex].focus();
  } else if (e.key === "ArrowDown") {
    e.preventDefault();
    focusedIndex = focusedIndex >= 6 ? focusedIndex - 6 : focusedIndex + 3;
    cells[focusedIndex].focus();
  } else if (e.key === "ArrowLeft") {
    e.preventDefault();
    focusedIndex = focusedIndex % 3 === 0 ? focusedIndex + 2 : focusedIndex - 1;
    cells[focusedIndex].focus();
  } else if (e.key === "ArrowRight") {
    e.preventDefault();
    focusedIndex = focusedIndex % 3 === 2 ? focusedIndex - 2 : focusedIndex + 1;
    cells[focusedIndex].focus();
  } else if (e.key === "Enter" || e.key === " ") {
    e.preventDefault();
    if (!cells[focusedIndex].disabled) {
      playMove(focusedIndex);
    }
  }
}

/**
 * Track which cell has focus for keyboard navigation.
 */
function handleCellFocus(e) {
  const cell = e.target;
  const index = parseInt(cell.dataset.index, 10);
  focusedIndex = index;
}

/**
 * Make the computer's move after a short delay.
 * The delay makes the move visible and feels more natural.
 */
async function makeComputerMove() {
  isThinking = true;
  render();

  // Wait for 300ms so the human can see what just happened
  await new Promise(resolve => setTimeout(resolve, 300));

  try {
    const computerMoveIndex = chooseMove(board, "O", difficulty);
    board = applyMove(board, computerMoveIndex);
  } catch (err) {
    // This shouldn't happen, but silently ignore
  } finally {
    isThinking = false;
    render();
  }
}

/**
 * Reset the game to a fresh state.
 */
function handleReset() {
  board = createBoard();
  isThinking = false;
  focusedIndex = 0;
  gameWasFinished = false;
  document.querySelectorAll(".cell").forEach((cell, index) => {
    cell.classList.remove("win");
    cell.classList.remove("mark");
    delete cell.dataset.marked;
    if (index === 0) {
      cell.focus();
    }
  });
  render();
}

/**
 * Handle mode change (two-player vs vs-computer).
 */
function handleModeChange(e) {
  gameMode = e.target.value;
  handleReset();
}

/**
 * Handle difficulty change.
 */
function handleDifficultyChange(e) {
  difficulty = e.target.value;
  handleReset();
}

/**
 * Reset scores to zero and clear localStorage.
 */
function handleResetScores() {
  scores = { x: 0, o: 0, draw: 0 };
  try {
    localStorage.removeItem(STORAGE_KEY);
  } catch (err) {
    // localStorage is unavailable - in-memory reset is enough
  }
  renderScoreboard();
}

// Set up event listeners
const boardEl = document.getElementById("board");
boardEl.addEventListener("click", handleCellClick);
boardEl.addEventListener("keydown", handleKeyDown);
boardEl.querySelectorAll(".cell").forEach(cell => {
  cell.addEventListener("focus", handleCellFocus);
});

document.getElementById("reset").addEventListener("click", handleReset);
document.getElementById("mode").addEventListener("change", handleModeChange);
document.getElementById("difficulty").addEventListener("change", handleDifficultyChange);
document.getElementById("reset-scores").addEventListener("click", handleResetScores);

// Load scores from localStorage
loadScores();

// Initial render
renderScoreboard();
render();

// Set initial focus to first cell
boardEl.querySelector(".cell").focus();
