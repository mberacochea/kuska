# achka

A playable tic-tac-toe game in vanilla JavaScript—no dependencies, no build step, no framework.

## What it is

**achka** is a minimal, browser-based tic-tac-toe game. Play against another human, or challenge an unbeatable computer opponent. The game is keyboard-accessible, remembers scores across sessions, and respects dark mode and reduced-motion preferences.

## How to run it

Browsers block ES modules over `file://` URLs, so the game must be served over HTTP:

```bash
python3 -m http.server 8080 --directory game
```

Then open http://localhost:8080 in your browser.

## How to test it

The game's rules engine and AI are pure functions with no DOM dependencies, so they can be tested in Node.js:

```bash
node --test game/tests/
```

This runs both the engine tests (all possible games) and AI tests (proving hard mode never loses).

## Files

- **`engine.js`** — Pure rules: board state, move validation, win/draw detection. Testable without a browser.
- **`ai.js`** — Computer opponent using minimax algorithm; difficulty levels (easy/medium/hard).
- **`game.js`** — The only file that touches the DOM; connects engine and AI to the UI.
- **`index.html`** — The page: board markup, controls, no build step needed.
- **`style.css`** — All styling: CSS grid board, light/dark mode, animations, responsive layout.
- **`tests/`** — Engine and AI test suites; run with `node --test`.

## Game modes

- **Two Player** — Play against a friend on the same device.
- **vs Computer** — Play against the AI at three difficulty levels:
  - **Easy** — Computer plays random moves.
  - **Medium** — Computer plays optimally 70% of the time, else random.
  - **Hard** — Computer always plays optimally (unbeatable).

## Keyboard controls

- Arrow keys navigate the 3×3 grid with wrap-around.
- Enter or Space plays the focused cell.

## Scoreboard

The game keeps a running tally of wins, losses, and draws, saved to `localStorage`. Click "Reset Scores" to clear it.
