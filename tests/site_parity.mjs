// Runs site/netback.js for each scenario and prints per-site best netbacks as JSON.
// Input on stdin: {"data": <site data.json>, "scenarios": [<inputs>, ...]}
import { evaluate, crossover } from "../site/netback.js";

let raw = "";
for await (const chunk of process.stdin) raw += chunk;
const { data, scenarios } = JSON.parse(raw);
const out = scenarios.map((inputs) => ({
  sites: Object.fromEntries(evaluate(data, inputs).sites.map((s) => [
    s.site.site_name,
    { hub: s.bestHub.netback, fuel: s.bestFuel.netback, best: s.best.label,
      be_hh: s.breakEvenHenryHub, be_discount: s.breakEvenDiscount },
  ])),
  crossover: crossover(data, inputs),
}));
process.stdout.write(JSON.stringify(out));
