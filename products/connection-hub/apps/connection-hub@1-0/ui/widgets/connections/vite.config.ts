import path from "node:path";
import {defineConfig} from "vite";
import react from "@vitejs/plugin-react";

// The platform's shared session helper arrives materialized under _shared/
// (the widget's `shared_sources` in the app descriptor: npm://components-core/src/session).
// KDCUBE_COMPONENTS_CORE_SRC points a local build at a checkout's
// npm/packages/components-core/src instead.
const componentsCoreSrc = process.env.KDCUBE_COMPONENTS_CORE_SRC
    ? path.resolve(process.env.KDCUBE_COMPONENTS_CORE_SRC)
    : path.resolve(__dirname, "_shared/components-core");

// The platform build invokes:
//   npm install --no-package-lock && OUTDIR=<VI_BUILD_DEST_ABSOLUTE_PATH> npm run build
// so the bundled assets must land in the directory named by OUTDIR.
export default defineConfig({
    plugins: [react()],
    resolve: {
        alias: {
            "@kdcube/components-core/session": path.join(componentsCoreSrc, "session/index.ts"),
        },
    },
    // Relative base so the built index.html + assets work under the widget's
    // mounted path (e.g. /api/integrations/bundles/<tenant>/<project>/<bundle>/...).
    base: "./",
    build: {
        outDir: process.env.OUTDIR || "dist",
        emptyOutDir: true,
    },
});
