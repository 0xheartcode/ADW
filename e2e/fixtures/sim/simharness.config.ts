// The example project's simharness config: adapter factory plus project
// staging, assertions, balance metrics and fuzz invariants, exactly the shape
// a real consumer ships. The generic harness (run/strategies/fuzz/autoplay/CLI)
// comes from @0xheartcode/simharness and reads only the universal GameState
// core; chase-specific state lives in `extra`.
//
// Drive it with the binary from this directory:
//   simharness assert
//   simharness autoplay 7 400 aggressive
//   simharness balance 8
//   simharness ascii 3
import type { AssertResult, MetricsReport, SimharnessConfig } from "@0xheartcode/simharness/config";
import type { GameState, SimAdapter } from "@0xheartcode/simharness/adapter";
import { run, strategies } from "@0xheartcode/simharness";
import { ChaseAdapter, type ChaseExtra } from "./chase_adapter";

type A = SimAdapter<ChaseExtra>;
type ChaseState = GameState<ChaseExtra>;

function asserts(a: A): AssertResult[] {
  const res: AssertResult[] = [];

  // A hero that never acts is overrun by the converging creeps.
  a.reset(5);
  const r1 = run(a, { seed: 5, ticks: 400, noReset: true, timeline: (_t, s) => strategies.idle(s, a) });
  res.push({ name: "idle hero is overrun", ok: r1.final.phase === "over", msg: `phase ${r1.final.phase}` });

  // Playing aggressively scores kills.
  a.reset(1);
  const r2 = run(a, { seed: 1, ticks: 400, noReset: true, timeline: (_t, s) => strategies.aggressive(s, a) });
  res.push({ name: "aggressive gets kills", ok: r2.final.extra.kills > 0, msg: `kills ${r2.final.extra.kills}` });

  // Same seed + same policy => identical run (deterministic replay).
  const tl = (_t: number, s: ChaseState) => strategies.aggressive(s, a);
  const g1 = run(a, { seed: 9, ticks: 300, timeline: tl });
  const g2 = run(a, { seed: 9, ticks: 300, timeline: tl });
  res.push({ name: "deterministic replay", ok: JSON.stringify(g1.final) === JSON.stringify(g2.final) });

  return res;
}

function metrics(a: A, o: { seeds: number; ticks: number }): MetricsReport {
  let survived = 0;
  let sumKills = 0;
  const rows: string[] = [];
  for (let i = 0; i < o.seeds; i++) {
    const seed = 1 + i * 13;
    const r = run(a, { seed, ticks: o.ticks, timeline: (_t, s) => strategies.aggressive(s, a) });
    const f = r.final;
    if (f.phase !== "over") survived++;
    sumKills += f.extra.kills;
    rows.push(`seed ${seed}: ${f.phase} · kills ${f.extra.kills} · tick ${f.tick}`);
  }
  return {
    summary: { seeds: o.seeds, winRate: +(survived / o.seeds).toFixed(2), avgKills: Math.round(sumKills / o.seeds) },
    rows,
  };
}

function invariants(s: ChaseState): string[] {
  const out: string[] = [];
  if (s.hero?.hp !== undefined && s.hero.hp < 0) out.push("negative hero hp");
  if (s.hero?.hp !== undefined && s.hero.maxHp !== undefined && s.hero.hp > s.hero.maxHp) out.push("hp exceeds max");
  if (s.extra.kills < 0) out.push("negative kills");
  return out;
}

const config: SimharnessConfig<ChaseExtra> = {
  createAdapter: () => new ChaseAdapter(),
  asserts,
  metrics,
  invariants,
};
export default config;
