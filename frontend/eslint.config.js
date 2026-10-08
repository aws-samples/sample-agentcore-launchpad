import js from "@eslint/js";
import reactHooks from "eslint-plugin-react-hooks";
import reactRefresh from "eslint-plugin-react-refresh";
import globals from "globals";
import tseslint from "typescript-eslint";

export default [
  // dist = build output; src/studio/lib = pure generator/validator files ported
  // verbatim from upstream strands_studio_ui (kept byte-faithful for re-syncing —
  // do not restyle). tsc still type-checks them under the project tsconfig.
  { ignores: ["dist", "src/studio/lib/**"] },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  {
    // node-side evidence tooling (playwright screenshot runs), not app code
    files: ["scripts/**/*.mjs"],
    languageOptions: { ecmaVersion: 2022, globals: { ...globals.node, ...globals.browser } },
  },
  {
    files: ["**/*.{ts,tsx}"],
    languageOptions: {
      ecmaVersion: 2022,
      globals: globals.browser,
    },
    plugins: {
      "react-hooks": reactHooks,
      "react-refresh": reactRefresh,
    },
    rules: {
      ...reactHooks.configs.recommended.rules,
      "react-refresh/only-export-components": ["warn", { allowConstantExport: true }],
    },
  },
];
