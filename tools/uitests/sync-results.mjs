/* A local Apply is not confirmation that every node applied the version. */
import "./stub-dom.mjs";
import { applySummary } from "../../static/js/shell.js";
import { openWizard } from "../../static/js/pages/services.js";
import { clusterCard } from "../../static/js/pages/cluster.js";

let fail = 0;
const ok = (c, m) => { console.log((c ? "  PASS  " : "  FAIL  ") + m); if (!c) fail++; };
const partial = {
  ok: false, local_ok: true, cluster_ok: false,
  error: "node-b has not confirmed the version",
  warnings: ["peer application needs attention"],
  sync: { ok: false, results: [{ name: "node-b", ok: false, error: "reload failed" }] },
};
let text = applySummary(partial);
ok(text.includes("Applied on this node.") && text.includes("Applied with problems"),
   "partial success names its local scope rather than claiming cluster success");
ok(text.includes("node-b") && text.includes("reload failed") &&
   text.includes("peer application needs attention"),
   "the peer's error and application warnings are visible");
ok(!text.includes("Applied."), "a partial result does not say Applied");
ok(applySummary({ ok: true, local_ok: true, cluster_ok: null }) === "Applied on this node.",
   "manual synchronization does not imply the other nodes applied");
ok(applySummary({ ok: true, local_ok: true, cluster_ok: true }) === "Applied.",
   "all-node confirmation can report full success");
ok(applySummary({ ok: false, error: "validation failed" }).includes("validation failed"),
   "a local failure keeps its reason");

globalThis.fetch = async (url) => {
  const path = String(url).replace(/^\/api\//, "").split("?")[0];
  const bodies = {
    "wizard/publish": { ok: true, actions: [], warnings: [], public: "", target: "",
                        preview: "", applied: partial },
    recipes: { recipes: [] }, services: [], traffic: { at: [], series: {} },
    probes: { results: [] },
    "acme/accounts": [], "acme/challenges": [], "acme/certificates": [],
    "access/groups": [], "haproxy/addresslists": [], "haproxy/rules": [],
    "haproxy/errorpages": [],
  };
  return { ok: true, status: 200, json: async () => bodies[path] ?? {}, text: async () => "" };
};
location.hash = "#/p:services";
openWizard({ url: "tcp://192.0.2.10:443", target: "tcp://10.0.0.20:443" });
const publish = document.querySelector("#dlgfoot").querySelectorAll("button")
  .find(b => b.textContent === "Publish");
await publish.onclick();
ok(document.querySelector("#dlgbody").text.includes("reload failed"),
   "the Services dialog renders nested peer application failures");

const node = {
  name: "node-b", hostname: "node-b", reachable: true, role: "passive",
  haproxy: "active", keepalived: "active", vips: [], vip_held: [],
  config_rev: 2, config_fp: "new", published_rev: 2, published_fp: "new",
  applied_rev: 1, applied_fp: "old", apply_error: "reload failed",
};
globalThis.fetch = async () => ({
  ok: true, status: 200, json: async () => ({
    nodes: [node], summary: { total: 1, reachable: 1, config_rev: 2,
      config_agreed: false, warnings: ["node-b application failed"] },
  }),
});
const card = await clusterCard();
ok(card.text.includes("reload failed") && card.text.includes("revision 1"),
   "the Cluster table shows the applied revision and the last failure");

console.log(fail ? `\n${fail} failed` : "\npublication results keep their true scope");
process.exit(fail ? 1 : 0);
