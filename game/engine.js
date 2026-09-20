export const EMPTY = "";
export const LINES = [
  [0, 1, 2], [3, 4, 5], [6, 7, 8],  // rows
  [0, 3, 6], [1, 4, 7], [2, 5, 8],  // cols
  [0, 4, 8], [2, 4, 6]               // diagonals
];

/** Create an empty board (9 cells). */
export function createBoard() {
  return Array(9).fill(EMPTY);
}

/** Derive whose turn it is from board state (X moves first). */
export function currentPlayer(board) {
  const xCount = board.filter(c => c === "X").length;
  const oCount = board.filter(c => c === "O").length;
  return xCount === oCount ? "X" : "O";
}

/** Return indices of empty cells. */
export function legalMoves(board) {
  return board
    .map((cell, i) => cell === EMPTY ? i : null)
    .filter(i => i !== null);
}

/** Apply a move to the board (returns a new array, throws on illegal move). */
export function applyMove(board, index) {
  if (index < 0 || index > 8 || !Number.isInteger(index)) {
    throw new Error(`Invalid index: ${index}`);
  }
  if (board[index] !== EMPTY) {
    throw new Error(`Cell ${index} is occupied`);
  }
  if (winner(board) !== null || isDraw(board)) {
    throw new Error("Game is already over");
  }

  const newBoard = [...board];
  newBoard[index] = currentPlayer(board);
  return newBoard;
}

/** Check for a winner; returns { player, line } or null. */
export function winner(board) {
  for (const line of LINES) {
    const [a, b, c] = line;
    if (board[a] !== EMPTY && board[a] === board[b] && board[b] === board[c]) {
      return { player: board[a], line };
    }
  }
  return null;
}

/** Check if the game is a draw (full board, no winner). */
export function isDraw(board) {
  return board.every(c => c !== EMPTY) && winner(board) === null;
}

/** Get the current game status { state, player, line }. */
export function status(board) {
  const w = winner(board);
  if (w) {
    return { state: "won", player: w.player, line: w.line };
  }
  if (isDraw(board)) {
    return { state: "draw", player: null, line: null };
  }
  return { state: "playing", player: currentPlayer(board), line: null };
}
