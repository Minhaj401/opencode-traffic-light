import { execFile } from "node:child_process";
import { homedir } from "node:os";
import { join } from "node:path";

let started = false;

export const TrafficLightLauncher = async () => {
  if (started) return {};
  started = true;
  if (process.platform === "darwin" || process.platform === "linux") {
    try {
      const child = execFile(
        join(homedir(), ".local", "bin", "traffic-light"),
        ["autostart"],
        { timeout: 3000 },
        () => {}
      );
      child.unref();
      for (const stream of [child.stdin, child.stdout, child.stderr]) {
        stream?.unref?.();
      }
    } catch {
      // Widget startup is optional and must not interrupt the application.
    }
  }
  return {};
};

export default TrafficLightLauncher;
