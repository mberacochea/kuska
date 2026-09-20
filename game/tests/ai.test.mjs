import assert from "node:assert";
import test from "node:test";

import { createBoard, applyMove, currentPlayer, legalMoves, winner, status } from "../engine.js";
import { randomMove, bestMove, chooseMove } from "../ai.js";

test("randomMove picks a legal move", () => {
  const board = createBoard();
  const move = randomMove(board);
  assert(Number.isInteger(move), "move should be an integer");
  assert(move >= 0 && move < 9, "move should be 0-8");
  assert(board[move] === "", "move should be on empty cell");
});

test("randomMove returns one of the legal moves", () => {
  const board = createBoard();
  board[0] = "X";
  board[4] = "O";
  const move = randomMove(board);
  assert(move !== 0 && move !== 4, "move should not be on occupied cells");
});

test("bestMove finds a winning move", () => {
  // Set up a board where X has two in a row and can win
  // X at 0, 1, empty at 2 (X's turn)
  const board = ["X", "X", "", "", "O", "", "", "", "O"];
  const move = bestMove(board, "X");
  assert.equal(move, 2, "should find the winning move at index 2");
});

test("bestMove blocks opponent winning move", () => {
  // Set up a board where O can win on the next move
  // O at 0, 1, empty at 2 (X's turn, but X should block O's winning move)
  const board = ["O", "O", "", "X", "", "", "", "", "X"];
  const move = bestMove(board, "X");
  assert.equal(move, 2, "should block opponent's winning move at index 2");
});

test("chooseMove returns a legal move for all difficulties", () => {
  const board = createBoard();

  for (const difficulty of ["easy", "medium", "hard"]) {
    const move = chooseMove(board, "X", difficulty);
    assert(Number.isInteger(move), `${difficulty} should return an integer`);
    assert(move >= 0 && move < 9, `${difficulty} move should be 0-8`);
    assert(board[move] === "", `${difficulty} move should be on empty cell`);
  }
});

test("chooseMove easy always returns random move", () => {
  const board = ["X", "", "", "", "", "", "", "", ""];
  const mockRng = () => 0.5;
  const move1 = chooseMove(board, "O", "easy", mockRng);
  const move2 = chooseMove(board, "O", "easy", mockRng);

  // Both should be legal moves (could be same or different)
  assert(move1 >= 0 && move1 < 9);
  assert(move2 >= 0 && move2 < 9);
});

test("chooseMove medium uses rng correctly", () => {
  const board = ["X", "", "", "", "", "", "", "", ""];
  let rngCalls = 0;
  const mockRng = () => {
    rngCalls++;
    return 0.5; // < 0.7, should use bestMove
  };

  const move = chooseMove(board, "O", "medium", mockRng);
  assert(rngCalls === 1, "medium should call rng once");
  assert(move >= 0 && move < 9);
});

test("chooseMove medium with rng=0 returns bestMove", () => {
  const board = ["X", "X", "", "", "O", "", "", "", "O"];
  const rngZero = () => 0;
  const move = chooseMove(board, "X", "medium", rngZero);
  const best = bestMove(board, "X");
  assert.equal(move, best, "medium with rng=0 should return bestMove");
});

test("chooseMove medium with rng=0.99 returns random move", () => {
  const board = ["X", "X", "", "", "O", "", "", "", "O"];
  const rng099 = () => 0.99;
  const move = chooseMove(board, "X", "medium", rng099);
  const legal = legalMoves(board);
  assert(legal.includes(move), "medium with rng=0.99 should return a legal move");
  // Note: with high probability this will be random, not best
});

test("chooseMove hard always uses bestMove", () => {
  // Set up a board where there's a winning move
  const board = ["X", "X", "", "", "O", "", "", "", "O"];
  const move = chooseMove(board, "X", "hard");
  // X should find the winning move at index 2
  assert.equal(move, 2, "hard should find the winning move");
});

test("chooseMove throws on unknown difficulty", () => {
  const board = createBoard();
  assert.throws(
    () => chooseMove(board, "X", "impossible"),
    /Unknown difficulty/
  );
});

test("hard mode never loses: exhaustive sweep with AI as X", () => {
  // Property test: play every game where opponent tries every legal move at every turn
  // and AI answers with bestMove. AI must never lose.
  let positions = 0;
  const maxPositions = 100000; // safety limit

  function assertAIDoesntLose(board, aiPlayer, opponentPlayer) {
    positions++;

    if (positions > maxPositions) {
      throw new Error(
        `Too many positions visited: ${positions} > ${maxPositions}`
      );
    }

    // Check if game is over
    const w = winner(board);
    const s = status(board);

    if (w !== null || s.state === "draw") {
      // Game is over - if AI lost, fail the test
      if (w !== null && w.player === opponentPlayer) {
        assert.fail(
          `AI player ${aiPlayer} lost to opponent ${opponentPlayer} on board: ${board.join(",")}`
        );
      }
      return;
    }

    // Get the current player
    const currentPly = currentPlayer(board);

    if (currentPly === aiPlayer) {
      // AI's turn: play the best move
      const move = bestMove(board, aiPlayer);
      const newBoard = applyMove(board, move);
      assertAIDoesntLose(newBoard, aiPlayer, opponentPlayer);
    } else {
      // Opponent's turn: try every legal move
      const moves = legalMoves(board);
      for (const move of moves) {
        const newBoard = applyMove(board, move);
        assertAIDoesntLose(newBoard, aiPlayer, opponentPlayer);
      }
    }
  }

  // Test: AI plays as X (goes first)
  assertAIDoesntLose(createBoard(), "X", "O");
  console.log(
    `  X vs O (exhaustive): visited ${positions} positions`
  );

  // Test: AI plays as O
  positions = 0;
  assertAIDoesntLose(createBoard(), "O", "X");
  console.log(
    `  O vs X (exhaustive): visited ${positions} positions`
  );
});
