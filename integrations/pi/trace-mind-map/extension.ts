// trace-mind-map: /map command for trace-mind.
//
// Install (from this directory's parent, or with the full path):
//   pi install ./integrations/pi/trace-mind-map        (global, ~/.pi/agent/settings.json)
//   pi install -l ./integrations/pi/trace-mind-map      (project-local, .pi/settings.json)
//
// This is a real installable pi package, not a loose file dropped into a
// folder -- confirmed against an actual installed Pi extension on this
// machine (pi-tray-notify) and the real @earendil-works/pi-coding-agent
// type definitions (v0.84.4, /opt/homebrew/lib/node_modules), not docs
// alone: a pi package is a directory with package.json declaring
// `"pi": {"extensions": ["./extension.ts"]}`, registered via `pi install
// <path>`, which is what package.json next to this file does.
//
// API surface confirmed directly against those real .d.ts files (not
// guessed from docs, unlike this file's first version):
//   - `pi.registerCommand(name, { description, handler })` where
//     `handler: (args: string, ctx: ExtensionCommandContext) => Promise<void>`
//     -- dist/core/extensions/types.d.ts:946, :891-895.
//   - `ExtensionCommandContext extends ExtensionContext`, and
//     `ExtensionContext.ui: ExtensionUIContext` (the FULL ui surface, not
//     a restricted Pick<>) -- so `ctx.ui.notify` is available here.
//   - `ExtensionUIContext.notify(message: string, type?: "info" | "warning"
//     | "error"): void` -- dist/core/extensions/types.d.ts:76.
//   - This pi version's ExtensionUIContext has no `custom()`/component API
//     for preformatted multi-line blocks (only select/confirm/input/notify/
//     setStatus/setWidget/setFooter/...) -- so notify() is the right fit
//     here, not a fallback guess.
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { execFileSync } from "node:child_process";

export default function (pi: ExtensionAPI) {
  pi.registerCommand("map", {
    description: "Show the trace-mind decision graph, current position highlighted",
    handler: async (_args, ctx) => {
      let output: string;
      try {
        // execFileSync (not execSync/shell) -- no shell involved, so no
        // command-injection surface even though this takes fixed args.
        output = execFileSync("trace-mind", ["map", "--color", "always"], {
          encoding: "utf8",
        });
      } catch (err) {
        ctx.ui.notify(
          `trace-mind map failed -- is trace-mind installed and is this project ingested? (${err})`,
          "error",
        );
        return;
      }
      ctx.ui.notify(output, "info");
    },
  });
}
