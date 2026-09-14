// Pi extension: /map command for trace-mind.
//
// Where to put this: `.pi/extensions/trace-mind-map.ts` (project-local) or
// `~/.pi/agent/extensions/trace-mind-map.ts` (global), per Pi's documented
// single-file extension layout (pi.dev/docs/latest/extensions).
//
// Confidence note, unlike the Codex/Claude Code integrations in this same
// directory: those two were checked against this machine's actual cloned
// reference source (reference/codex, reference/claude-code). No Pi source
// is available to check the same way -- this file is built only from Pi's
// published docs, fetched at write time. `pi.registerCommand()` and
// `ctx.ui.notify(message, level)` are directly, verbatim confirmed by that
// fetch. Pi also documents a richer `ctx.ui.custom()` for full TUI
// components, which would likely render a fixed-width ASCII tree more
// faithfully than notify()'s presumably-wrapped text -- but its exact
// component API wasn't confirmed from docs alone, so this file
// deliberately doesn't guess at it. If notify() turns out to wrap/strip
// this output in practice, that's the first thing to revisit -- confirm
// ctx.ui.custom()'s real signature against a live Pi install, not by
// guessing further from docs.
//
// Also unverified: this is a single-file extension with no package.json,
// so there's no node_modules for the type-only import below to resolve
// against. Whether Pi provides `@earendil-works/pi-coding-agent`'s types
// ambiently for single-file extensions wasn't confirmed either -- if it
// fails to resolve, drop the import and type `pi`/`ctx` as `any` instead;
// it's a type-only import, so removing it changes nothing at runtime.
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
