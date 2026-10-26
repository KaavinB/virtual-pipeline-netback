// Netback for the latest priced day under what-if inputs: the browser twin of
// mart.site_latest_scenario(mart.dashboard_overrides(inputs)).
//
// Every cost component comes from the SQL views via data.json (dashboards/site/export.py);
// this file only re-does the last step of arithmetic so the static page can answer
// "what if?" without a database. tests/test_site_parity.py runs it under Node and checks it
// against the SQL for a set of scenarios, so the two can't silently drift apart.

/** Inputs as a person enters them; null/undefined = base case. */
export function resolveInputs(data, inputs = {}) {
  const pick = (k, base) => (inputs[k] === null || inputs[k] === undefined || inputs[k] === "" ? base : Number(inputs[k]));
  return {
    henryHub: pick("henry_hub", data.latest.henry_hub),
    diesel: pick("diesel", data.latest.diesel),
    discountPct: pick("discount_pct", data.base.discount_pct),
    wahaDiscount: pick("waha_discount", data.base.waha_discount),
    customerMi: pick("customer_mi", data.base.customer_mi),
  };
}

/** $/MMBtu = fixed + round-trip miles x (diesel $/gal / mpg + non-fuel $/mile) / MMBtu per load */
export function truckingUsdPerMmbtu(mode, roundTripMi, diesel, c) {
  return mode.fixed_transport_usd_mmbtu
    + roundTripMi * (diesel / c.mpg + c.nonfuel_usd_per_mile) / mode.net_mmbtu_per_load;
}

export function dieselParity(data, diesel) {
  return diesel / data.constants.parity_mmbtu_per_gal;
}

function hubPrice(data, hub, x) {
  const basis = hub === "WAHA" ? -x.wahaDiscount : data.basis[hub];
  return x.henryHub + basis;
}

const HUB_LABEL = { WAHA: "Waha", AGUA_DULCE: "Agua Dulce", KATY: "Katy" };

/** Every option for one site: 3 hubs x 2 modes + fuel customer x 2 modes. */
export function siteOptions(data, site, x) {
  const c = data.constants;
  const options = [];
  for (const mode of data.modes) {
    const legs = [
      ...Object.entries(site.hub_miles).map(([hub, mi]) => ({ market: "hub", hub, oneWay: mi })),
      { market: "fuel", hub: null, oneWay: x.customerMi },
    ];
    for (const leg of legs) {
      const price = leg.market === "hub"
        ? hubPrice(data, leg.hub, x)
        : dieselParity(data, x.diesel) * (1 - x.discountPct / 100);
      const roundTrip = 2 * (leg.oneWay + site.last_mile_mi);
      const trucking = truckingUsdPerMmbtu(mode, roundTrip, x.diesel, c);
      const delivery = leg.market === "hub" ? mode.delivery_usd_mmbtu : mode.delivery_as_fuel_usd_mmbtu;
      const cost = mode.conditioning_usd_mmbtu + trucking + delivery + mode.fee_usd_mmbtu;
      options.push({
        market: leg.market, hub: leg.hub, mode: mode.mode, oneWayMi: leg.oneWay,
        label: `${mode.mode} → ${leg.hub ? HUB_LABEL[leg.hub] : "fuel customer"}`,
        price, conditioning: mode.conditioning_usd_mmbtu, trucking, delivery,
        fee: mode.fee_usd_mmbtu, cost, netback: price - cost,
      });
    }
  }
  return options;
}

const best = (opts) => opts.reduce((a, b) => (b.netback > a.netback ? b : a));

/** One result per site, mirroring mart.v_site_latest. */
export function evaluate(data, inputs = {}) {
  const x = resolveInputs(data, inputs);
  const mmbtuPerDay = (site) => site.flared_mmscfd * 1000 * data.constants.mmbtu_per_mcf;
  const sites = data.sites.map((site) => {
    const options = siteOptions(data, site, x);
    const bestAll = best(options);
    const bestHub = best(options.filter((o) => o.market === "hub"));
    const bestFuel = best(options.filter((o) => o.market === "fuel"));
    return {
      site, options, best: bestAll, bestHub, bestFuel,
      annualValueUsd: bestAll.netback * mmbtuPerDay(site) * 365,
      // Henry Hub at which the best hub option nets $0: its costs minus its basis.
      breakEvenHenryHub: bestHub.cost - (bestHub.price - x.henryHub),
      // Discount off diesel parity at which the best fuel option nets $0.
      breakEvenDiscount: 1 - bestFuel.cost / dieselParity(data, x.diesel),
    };
  });
  return { inputs: x, sites };
}

/** Netback vs one-way distance for the dashboard's four reference options (no last mile). */
export function distanceCurve(data, inputs = {}, maxMi = 1000, step = 10) {
  const x = resolveInputs(data, inputs);
  const c = data.constants;
  const mode = Object.fromEntries(data.modes.map((m) => [m.mode, m]));
  const series = [
    { label: "CNG → fuel customer", mode: "CNG", market: "fuel" },
    { label: "LNG → fuel customer", mode: "LNG", market: "fuel" },
    { label: "CNG → Agua Dulce", mode: "CNG", market: "hub", hub: "AGUA_DULCE" },
    { label: "CNG → Waha", mode: "CNG", market: "hub", hub: "WAHA" },
  ];
  const miles = [];
  for (let mi = 0; mi <= maxMi; mi += step) miles.push(mi);
  return {
    miles,
    series: series.map((s) => {
      const m = mode[s.mode];
      const price = s.market === "hub"
        ? hubPrice(data, s.hub, x)
        : dieselParity(data, x.diesel) * (1 - x.discountPct / 100);
      const delivery = s.market === "hub" ? m.delivery_usd_mmbtu : m.delivery_as_fuel_usd_mmbtu;
      const fixed = m.conditioning_usd_mmbtu + delivery + m.fee_usd_mmbtu;
      return {
        ...s,
        values: miles.map((mi) => price - fixed - truckingUsdPerMmbtu(m, 2 * mi, x.diesel, c)),
      };
    }),
  };
}

/** CNG vs LNG cost lines (a + b x miles) and where they cross, for each market. */
export function crossover(data, inputs = {}) {
  const x = resolveInputs(data, inputs);
  const c = data.constants;
  const perMile = 2 * (x.diesel / c.mpg + c.nonfuel_usd_per_mile);
  const line = (m, market) => ({
    a: m.conditioning_usd_mmbtu + m.fee_usd_mmbtu + m.fixed_transport_usd_mmbtu
      + (market === "hub" ? m.delivery_usd_mmbtu : m.delivery_as_fuel_usd_mmbtu),
    b: perMile / m.net_mmbtu_per_load,
  });
  const cng = data.modes.find((m) => m.mode === "CNG");
  const lng = data.modes.find((m) => m.mode === "LNG");
  return ["fuel", "hub"].map((market) => {
    const C = line(cng, market), L = line(lng, market);
    return { market, cngFixed: C.a, lngFixed: L.a, miles: (L.a - C.a) / (C.b - L.b) };
  });
}
