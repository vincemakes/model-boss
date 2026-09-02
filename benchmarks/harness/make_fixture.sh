#!/usr/bin/env bash
# Build the benchmark fixture template: a small pnpm TypeScript project with
# `pnpm typecheck` (tsc --noEmit) and `pnpm test` (vitest) gates, git-initialized.
# Usage: make_fixture.sh <template-dir>
set -euo pipefail
T="${1:?template dir required}"
rm -rf "$T"
mkdir -p "$T/src/users" "$T/src/api" "$T/tests"
cd "$T"
cat > package.json <<'JSON'
{
  "name": "taskboard-fixture",
  "private": true,
  "type": "module",
  "scripts": {
    "typecheck": "tsc --noEmit",
    "test": "vitest run"
  },
  "devDependencies": {
    "typescript": "^5.6.0",
    "vitest": "^2.1.0",
    "@types/node": "^22.0.0"
  }
}
JSON
cat > tsconfig.json <<'JSON'
{
  "compilerOptions": {
    "target": "ES2022",
    "module": "ESNext",
    "moduleResolution": "Bundler",
    "strict": true,
    "noUncheckedIndexedAccess": true,
    "exactOptionalPropertyTypes": true,
    "skipLibCheck": true,
    "types": ["node"],
    "noEmit": true
  },
  "include": ["src", "tests"]
}
JSON
cat > vitest.config.ts <<'TS'
import { defineConfig } from "vitest/config";
export default defineConfig({ test: { include: ["tests/**/*.test.ts"] } });
TS
cat > src/users/user.ts <<'TS'
export interface User {
  id: string;
  name: string;
  email: string;
}

const users: Record<string, User> = {
  u1: { id: "u1", name: "Ada", email: "ada@example.com" },
  u2: { id: "u2", name: "Linus", email: "linus@example.com" },
};

export function fetchUserData(id: string): User | undefined {
  return users[id];
}
TS
cat > src/users/profile.ts <<'TS'
import { fetchUserData } from "./user";

export function displayName(id: string): string {
  const user = fetchUserData(id);
  return user ? `${user.name} <${user.email}>` : "unknown";
}
TS
cat > src/api/handlers.ts <<'TS'
import { fetchUserData } from "../users/user";

export function getUser(id: string) {
  const user = fetchUserData(id);
  if (!user) {
    throw new Error("not found");
  }
  return user;
}

export function listUsers() {
  return { users: ["u1", "u2"].map((id) => fetchUserData(id)) };
}
TS
cat > tests/users.test.ts <<'TS'
import { describe, expect, it } from "vitest";
import { fetchUserData } from "../src/users/user";

describe("fetchUserData", () => {
  it("returns a known user", () => {
    expect(fetchUserData("u1")?.name).toBe("Ada");
  });
  it("returns undefined for unknown ids", () => {
    expect(fetchUserData("nope")).toBeUndefined();
  });
});
TS
cat > tests/profile.test.ts <<'TS'
import { describe, expect, it } from "vitest";
import { displayName } from "../src/users/profile";

describe("displayName", () => {
  it("formats a known user", () => {
    expect(displayName("u2")).toBe("Linus <linus@example.com>");
  });
  it("falls back for unknown users", () => {
    expect(displayName("x")).toBe("unknown");
  });
});
TS
printf 'node_modules\n' > .gitignore
pnpm install --silent
pnpm typecheck >/dev/null
pnpm test >/dev/null
git init -q
git add -A
git -c user.name=bench -c user.email=bench@example.com commit -qm "fixture baseline"
echo "fixture template ready at $T"
