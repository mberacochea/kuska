// Computer opponent AI using minimax algorithm
// Pure module: no DOM, no side effects

import {
  EMPTY,
  currentPlayer,
  legalMoves,
  applyMove,
  winner,
  isDraw,
  status
} from "./engine.js";

/**
 * Pick a random legal move from the board.
 */
export function randomMove(board) {
  const moves = legalMoves(board);
  return moves[Math.floor(Math.random() * moves.length)];
}

/**
 * Find the best move using minimax algorithm.
 * Scoring: +10 for win, -10 for loss, 0 for draw, minus depth (prefer fast win/slow loss).
 */
export function bestMove(board, player) {
  let bestScore = -Infinity;
  let bestIndex = null;

  const moves = legalMoves(board);
  for (const move of moves) {
    const newBoard = applyMove(board, move);
    const score = minimax(newBoard, 0, player);
    if (score > bestScore) {
      bestScore = score;
      bestIndex = move;
    }
  }

  return bestIndex;
}

/**
 * Minimax helper: recursively evaluate board states.
 * Depth is subtracted from terminal scores to prefer faster wins and slower losses.
 */
function minimax(board, depth, maximizingPlayer) {
  // Terminal states: check for win or draw
  const gameStatus = status(board);

  if (gameStatus.state === "won") {
    const score = gameStatus.player === maximizingPlayer ? 10 : -10;
    return score - depth;
  }

  if (gameStatus.state === "draw") {
    return -depth; // Slight penalty for depth, but draw is neutral
  }

  // Recursive case: try all moves
  const moves = legalMoves(board);
  const isMaximizing = currentPlayer(board) === maximizingPlayer;

  if (isMaximizing) {
    let maxScore = -Infinity;
    for (const move of moves) {
      const newBoard = applyMove(board, move);
      const score = minimax(newBoard, depth + 1, maximizingPlayer);
      maxScore = Math.max(maxScore, score);
    }
    return maxScore;
  } else {
    let minScore = Infinity;
    for (const move of moves) {
      const newBoard = applyMove(board, move);
      const score = minimax(newBoard, depth + 1, maximizingPlayer);
      minScore = Math.min(minScore, score);
    }
    return minScore;
  }
}

/**
 * Choose a move based on difficulty level.
 * - easy: random move
 * - medium: bestMove 70% of the time, else random
 * - hard: always bestMove
 *
 * @param {string} difficulty - "easy" | "medium" | "hard"
 * @param {function} rng - injectable random function for testing (default Math.random)
 */
export function chooseMove(board, player, difficulty, rng = Math.random) {
  switch (difficulty) {
    case "easy":
      return randomMove(board);
    case "medium":
      return rng() < 0.7 ? bestMove(board, player) : randomMove(board);
    case "hard":
      return bestMove(board, player);
    default:
      throw new Error(`Unknown difficulty: ${difficulty}`);
  }
}
