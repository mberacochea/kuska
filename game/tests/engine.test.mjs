import test from "node:test";
import assert from "node:assert/strict";
import {
  EMPTY,
  LINES,
  createBoard,
  currentPlayer,
  legalMoves,
  applyMove,
  winner,
  isDraw,
  status,
} from "../engine.js";

test("createBoard gives nine empty cells", () => {
  const board = createBoard();
  assert.equal(board.length, 9, "board should have 9 cells");
  assert.deepEqual(board, Array(9).fill(EMPTY), "all cells should be empty");
});

test("currentPlayer alternates X, O, X...", () => {
  let board = createBoard();
  assert.equal(currentPlayer(board), "X", "X starts");

  // After X plays at index 0
  board = applyMove(board, 0);
  assert.equal(currentPlayer(board), "O", "O plays after X");

  // After O plays at index 1
  board = applyMove(board, 1);
  assert.equal(currentPlayer(board), "X", "X plays after O");

  // After X plays at index 2
  board = applyMove(board, 2);
  assert.equal(currentPlayer(board), "O", "O plays next");
});

test("legalMoves shrinks as marks are placed", () => {
  let board = createBoard();
  assert.equal(
    legalMoves(board).length,
    9,
    "empty board has 9 legal moves"
  );

  board = applyMove(board, 0);
  assert.equal(
    legalMoves(board).length,
    8,
    "8 legal moves after one move"
  );
  assert.ok(
    !legalMoves(board).includes(0),
    "index 0 should not be in legal moves"
  );

  board = applyMove(board, 1);
  assert.equal(
    legalMoves(board).length,
    7,
    "7 legal moves after two moves"
  );

  // Fill the board with a draw sequence: 0, 1, 2, 3, 4, 6, 5, 8, 7
  board = applyMove(board, 2);
  board = applyMove(board, 3);
  board = applyMove(board, 4);
  board = applyMove(board, 6);
  board = applyMove(board, 5);
  board = applyMove(board, 8);
  board = applyMove(board, 7);

  assert.equal(
    legalMoves(board).length,
    0,
    "full board has no legal moves"
  );
});

test("applyMove does not mutate input", () => {
  const board = createBoard();
  const originalBoard = [...board];

  applyMove(board, 0);

  assert.deepEqual(
    board,
    originalBoard,
    "original board should not be mutated"
  );
});

test("applyMove throws on invalid indices", () => {
  const board = createBoard();

  assert.throws(
    () => applyMove(board, -1),
    /Invalid index/,
    "should throw on negative index"
  );

  assert.throws(
    () => applyMove(board, 9),
    /Invalid index/,
    "should throw on index >= 9"
  );

  assert.throws(
    () => applyMove(board, 10),
    /Invalid index/,
    "should throw on index > 9"
  );
});

test("applyMove throws on occupied cell", () => {
  let board = createBoard();
  board = applyMove(board, 0);

  assert.throws(
    () => applyMove(board, 0),
    /occupied/,
    "should throw when cell is occupied"
  );
});

test("applyMove throws on board with existing winner", () => {
  let board = createBoard();

  // Create a winning position for X (top row: 0, 1, 2)
  board = applyMove(board, 0); // X at 0
  board = applyMove(board, 3); // O at 3
  board = applyMove(board, 1); // X at 1
  board = applyMove(board, 4); // O at 4
  board = applyMove(board, 2); // X at 2 - X wins

  assert.ok(winner(board) !== null, "X should have won");

  assert.throws(
    () => applyMove(board, 5),
    /already over/,
    "should throw when trying to play on a finished board"
  );
});

test("winner finds all eight lines correctly", () => {
  // Test each of the 8 lines
  const testCases = [
    { line: [0, 1, 2], moves: [0, 3, 1, 4, 2] }, // top row
    { line: [3, 4, 5], moves: [3, 0, 4, 1, 5] }, // middle row
    { line: [6, 7, 8], moves: [6, 0, 7, 1, 8] }, // bottom row
    { line: [0, 3, 6], moves: [0, 1, 3, 2, 6] }, // left col
    { line: [1, 4, 7], moves: [1, 0, 4, 2, 7] }, // middle col
    { line: [2, 5, 8], moves: [2, 0, 5, 1, 8] }, // right col
    { line: [0, 4, 8], moves: [0, 1, 4, 2, 8] }, // \ diagonal
    { line: [2, 4, 6], moves: [2, 0, 4, 1, 6] }, // / diagonal
  ];

  for (const { line, moves } of testCases) {
    let board = createBoard();
    for (const move of moves) {
      board = applyMove(board, move);
    }

    const w = winner(board);
    assert.ok(w !== null, `should find winner for line ${line}`);
    assert.equal(w.player, "X", "X should be the winner");
    assert.deepEqual(
      w.line,
      line,
      `line should be ${line.join(",")} not ${w.line.join(",")}`
    );
  }
});

test("winner returns null for empty board", () => {
  const board = createBoard();
  assert.equal(winner(board), null, "empty board has no winner");
});

test("winner returns null for board with no line", () => {
  let board = createBoard();

  // Play moves that don't form any line
  board = applyMove(board, 0); // X at 0
  board = applyMove(board, 1); // O at 1
  board = applyMove(board, 3); // X at 3
  board = applyMove(board, 2); // O at 2
  board = applyMove(board, 4); // X at 4
  board = applyMove(board, 5); // O at 5

  assert.equal(winner(board), null, "no winning line yet");
});

test("isDraw is true only on full board with no winner", () => {
  let board = createBoard();
  assert.equal(isDraw(board), false, "empty board is not a draw");

  // Play moves: 0, 1, 2, 3, 4, 6, 5, 8, 7 (verified draw sequence)
  board = applyMove(board, 0);
  assert.equal(isDraw(board), false, "1 move is not a draw");

  board = applyMove(board, 1);
  board = applyMove(board, 2);
  board = applyMove(board, 3);
  board = applyMove(board, 4);
  board = applyMove(board, 6);
  board = applyMove(board, 5);
  board = applyMove(board, 8);
  board = applyMove(board, 7);

  assert.equal(isDraw(board), true, "full board with no winner is a draw");
});

test("status never reports both won and draw", () => {
  let board = createBoard();
  let s = status(board);
  assert.equal(s.state, "playing", "empty board is playing");
  assert.ok(
    !(s.state === "won" && s.state === "draw"),
    "cannot be both won and draw"
  );

  // Create a winning board
  board = applyMove(board, 0); // X at 0
  board = applyMove(board, 3); // O at 3
  board = applyMove(board, 1); // X at 1
  board = applyMove(board, 4); // O at 4
  board = applyMove(board, 2); // X at 2 - X wins

  s = status(board);
  assert.equal(s.state, "won", "board with winner reports won");
  assert.equal(s.player, "X", "winner is X");
  assert.deepEqual(s.line, [0, 1, 2], "line is top row");
  assert.ok(
    !(s.state === "won" && s.state === "draw"),
    "cannot be both won and draw"
  );

  // Create a draw board using sequence: 0, 1, 2, 3, 4, 6, 5, 8, 7
  board = createBoard();
  board = applyMove(board, 0);
  board = applyMove(board, 1);
  board = applyMove(board, 2);
  board = applyMove(board, 3);
  board = applyMove(board, 4);
  board = applyMove(board, 6);
  board = applyMove(board, 5);
  board = applyMove(board, 8);
  board = applyMove(board, 7);

  s = status(board);
  assert.equal(s.state, "draw", "full board with no winner reports draw");
  assert.equal(s.player, null, "draw has no player");
  assert.equal(s.line, null, "draw has no line");
  assert.ok(
    !(s.state === "won" && s.state === "draw"),
    "cannot be both won and draw"
  );
});

test("exhaustive sweep: play every possible game", () => {
  let nodeCount = 0;
  const maxNodes = 600000; // expect ~550k, set higher for safety

  function playAllGames(board) {
    nodeCount++;

    if (nodeCount > maxNodes) {
      throw new Error(
        `Too many nodes visited: ${nodeCount} > ${maxNodes}`
      );
    }

    // Check invariants at every position
    const w = winner(board);
    const d = isDraw(board);
    const s = status(board);

    // At most one player can have a winning line
    let xWins = false;
    let oWins = false;
    if (w !== null) {
      if (w.player === "X") {
        xWins = true;
      } else if (w.player === "O") {
        oWins = true;
      }
    }
    assert.ok(
      !(xWins && oWins),
      "cannot have both X and O winning at the same position"
    );

    // status should never report both won and draw
    assert.ok(
      !(s.state === "won" && s.state === "draw"),
      "status cannot report both won and draw"
    );

    // If there's a winner, status.state must be "won"
    if (w !== null) {
      assert.equal(s.state, "won", "status should be won when winner exists");
      assert.equal(
        s.player,
        w.player,
        "status player should match winner player"
      );
      assert.deepEqual(
        s.line,
        w.line,
        "status line should match winner line"
      );
    }

    // If it's a draw, status.state must be "draw"
    if (d) {
      assert.equal(
        s.state,
        "draw",
        "status should be draw when board is full"
      );
      assert.equal(s.player, null, "draw status has no player");
      assert.equal(s.line, null, "draw status has no line");
    }

    // If game is over (won or draw), don't recurse
    if (w !== null || d) {
      return;
    }

    // Recurse on all legal moves
    const moves = legalMoves(board);
    for (const move of moves) {
      const newBoard = applyMove(board, move);
      playAllGames(newBoard);
    }
  }

  playAllGames(createBoard());
  console.log(`  Visited ${nodeCount} game positions`);
  assert.ok(nodeCount < maxNodes, `visited ${nodeCount} nodes (< ${maxNodes})`);
});
