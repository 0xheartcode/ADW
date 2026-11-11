// A tiny, fully deterministic game that implements SimAdapter, the example the
// harness drives in its own tests and in the "try it in a minute" quickstart.
// Nothing here is game-specific to the harness: it only sees the universal
// GameState core plus `extra`. The sim owns its RNG (seeded), so a run is
// bit-identical for a given seed and input policy, which is what the harness's
// deterministic-replay assertion checks.
import type { EntityState, GameState, InputState, SimAdapter } from "@0xheartcode/simharness/adapter";

/** The chase game's project-specific state, carried in GameState.extra. */
export interface ChaseExtra extends Record<string, unknown> {
  kills: number;
  spawned: number;
}

const W = 24;
const H = 12;
const FIRE_RANGE = 3;

/** mulberry32: a small, fast, seedable PRNG so runs are reproducible. */
function rng(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a |= 0;
    a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

export class ChaseAdapter implements SimAdapter<ChaseExtra> {
  readonly name = "chase";

  private rand: () => number = rng(1);
  private tick = 0;
  private phase = "running";
  private hero = { x: W / 2, y: H / 2, hp: 10, maxHp: 10, down: false };
  private enemies: EntityState[] = [];
  private kills = 0;
  private spawned = 0;
  private input: InputState = {};

  reset(seed: number, _opts?: Record<string, unknown>): void {
    this.rand = rng(seed);
    this.tick = 0;
    this.phase = "running";
    this.hero = { x: W / 2, y: H / 2, hp: 10, maxHp: 10, down: false };
    this.enemies = [];
    this.kills = 0;
    this.spawned = 0;
    this.input = {};
    for (let i = 0; i < 4; i++) this.spawn();
  }

  private spawn(): void {
    const id = `e${this.spawned++}`;
    this.enemies.push({
      kind: "creep",
      id,
      x: Math.floor(this.rand() * W),
      y: Math.floor(this.rand() * H),
      hp: 1,
    });
  }

  setInput(partial: InputState): void {
    this.input = { ...this.input, ...partial };
  }

  step(_dt: number): void {
    if (this.phase !== "running") return;
    this.tick++;

    // Hero moves per held input (clamped to the board).
    const mv = this.input.move ?? { x: 0, y: 0 };
    this.hero.x = clamp(this.hero.x + Math.sign(mv.x), 0, W - 1);
    this.hero.y = clamp(this.hero.y + Math.sign(mv.y), 0, H - 1);

    // Firing removes the nearest enemy within range (deterministic: lowest id
    // wins a tie, since the list keeps spawn order).
    if (this.input.fire && this.enemies.length) {
      let best = -1;
      let bestD = Infinity;
      for (let i = 0; i < this.enemies.length; i++) {
        const d = dist2(this.enemies[i], this.hero);
        if (d < bestD && d <= FIRE_RANGE * FIRE_RANGE) {
          bestD = d;
          best = i;
        }
      }
      if (best >= 0) {
        this.enemies.splice(best, 1);
        this.kills++;
      }
    }

    // Enemies step one cell toward the hero; contact costs the hero 1 hp.
    for (const e of this.enemies) {
      e.x += Math.sign(this.hero.x - e.x);
      e.y += Math.sign(this.hero.y - e.y);
      if (e.x === this.hero.x && e.y === this.hero.y) this.hero.hp = (this.hero.hp ?? 0) - 1;
    }

    if ((this.hero.hp ?? 0) <= 0) {
      this.hero.hp = 0;
      this.hero.down = true;
      this.phase = "over";
    }
  }

  readState(): GameState<ChaseExtra> {
    return {
      tick: this.tick,
      phase: this.phase,
      hero: { ...this.hero },
      enemies: this.enemies.map((e) => ({ ...e })),
      extra: { kills: this.kills, spawned: this.spawned },
    };
  }

  snapshotAscii(): string {
    const grid: string[][] = Array.from({ length: H }, () => Array.from({ length: W }, () => "."));
    for (const e of this.enemies) if (inBounds(e.x, e.y)) grid[e.y][e.x] = "x";
    if (inBounds(this.hero.x, this.hero.y)) grid[this.hero.y][this.hero.x] = this.hero.down ? "X" : "@";
    const board = grid.map((row) => row.join("")).join("\n");
    return `tick ${this.tick}  phase ${this.phase}  hp ${this.hero.hp}  kills ${this.kills}\n${board}`;
  }

  debug(action: string, _arg?: unknown): void {
    switch (action) {
      case "spawn":
        this.spawn();
        break;
      case "clear":
        this.enemies = [];
        break;
      case "hurt":
        this.hero.hp = (this.hero.hp ?? 0) - 1;
        break;
    }
  }
}

function clamp(v: number, lo: number, hi: number): number {
  return v < lo ? lo : v > hi ? hi : v;
}
function dist2(a: { x: number; y: number }, b: { x: number; y: number }): number {
  return (a.x - b.x) ** 2 + (a.y - b.y) ** 2;
}
function inBounds(x: number, y: number): boolean {
  return x >= 0 && x < W && y >= 0 && y < H;
}
