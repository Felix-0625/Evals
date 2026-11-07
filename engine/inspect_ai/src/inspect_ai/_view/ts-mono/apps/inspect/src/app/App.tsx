import "bootstrap/dist/css/bootstrap.css";
import "prismjs/themes/prism.css";
import "@tsmono/theme/base";
import "@tsmono/theme/vscode";
import "./App.css";

import { QueryClientProvider } from "@tanstack/react-query";
import {
  FC,
  lazy,
  Suspense,
  useCallback,
  useLayoutEffect,
  useState,
} from "react";
import { RouterProvider } from "react-router/dom";

import { useEventListener } from "@tsmono/react/hooks";
import { getVscodeApi } from "@tsmono/util";

import {
  AppConfigGate,
  LogLocationGate,
  readEmbeddedStartupState,
  resolveEmbeddedLogDir,
  setLogRoot,
} from "../app_config";
import { HostMessage } from "../client/api/types.ts";
import { webviewStorage } from "../client/storage";
import { FetchEngineController, imperativeLogData } from "../log_data";
import { queryClient } from "../state/queryClient.ts";
import {
  SETTINGS_STORAGE_KEY,
  useUserSettings,
} from "../state/userSettings.ts";

import { AppProviders } from "./AppProviders.tsx";
import { getAppRouter } from "./routing/AppRouter.tsx";
import {
  createHostCommandFilter,
  hostDestinationRoute,
} from "./routing/hostNavigation";

/**
 * Keep the applied theme in lockstep with the persisted preference. The inline
 * bootstrap sets the theme before first paint from localStorage; here we
 * re-apply whenever the in-app picker changes it, and pull cross-tab writes
 * back into zustand (persist doesn't listen for `storage` events itself).
 *
 * A render-null controller at the composition root — an irreducible effect
 * (DOM/theme + storage-event bridging), not data.
 */
const ThemePreferenceSyncController: FC = () => {
  const themePreference = useUserSettings((s) => s.themePreference);
  // useLayoutEffect (not useEffect): apply before the browser paints so an
  // in-tab pick flips the CSS in the same frame the toggle re-renders. With a
  // post-paint effect the icon updates a frame before the colors, flashing the
  // old theme. (The old bespoke hook applied synchronously on write.)
  // eslint-disable-next-line tsmono/no-raw-use-effect -- baselined at rule introduction; migrate to a named hook or derived state
  useLayoutEffect(() => {
    window.__APPLY_BROWSER_THEME__?.();
  }, [themePreference]);

  useEventListener(window, "storage", (e) => {
    if (e.key === SETTINGS_STORAGE_KEY) {
      // Re-apply CSS for this tab (the bootstrap reads the freshly-written
      // value) and pull the new preference into the store.
      window.__APPLY_BROWSER_THEME__?.();
      // eslint-disable-next-line @typescript-eslint/no-floating-promises -- rehydrate never rejects: zustand routes its errors to onRehydrateStorage, and its anonymous type can't be listed in allowForKnownSafeCalls
      useUserSettings.persist.rehydrate();
    }
  });

  return null;
};

/**
 * Renders the application content. Mounted below the config gate so it can
 * read the resolved app config.
 */
export const AppContent: FC = () => {
  const [router] = useState(getAppRouter);
  const [isNewHostCommand] = useState(() =>
    createHostCommandFilter(webviewStorage, readEmbeddedStartupState())
  );

  const onMessage = useCallback(
    (e: HostMessage) => {
      switch (e.data.type) {
        case "updateState": {
          if (e.data.url && isNewHostCommand(e.data)) {
            const decodedUrl = decodeURIComponent(e.data.url);

            // Update the resolved log dir for host-driven (live) navigation —
            // the one place logDir changes after the gate. The initial embedded
            // dir is already seeded by the log-root resolution at startup.
            setLogRoot(resolveEmbeddedLogDir(decodedUrl));

            router.navigate(hostDestinationRoute(e.data), { replace: true });
          }
          break;
        }
        case "backgroundUpdate": {
          imperativeLogData.invalidateLogListing();
          break;
        }
      }
    },
    [router, isNewHostCommand]
  );

  // Only the VS Code host may drive the log location. A window message can't
  // be authenticated (any page embedding the viewer can post one), so outside
  // VS Code the bridge is never attached (#615).
  useEventListener(getVscodeApi() ? window : null, "message", onMessage);

  return (
    <>
      <ThemePreferenceSyncController />
      <FetchEngineController />
      <AppProviders>
        <RouterProvider router={router} />
      </AppProviders>
    </>
  );
};

/**
 * Renders the Main Application. Owns the query client provider so the
 * exported <App> stays self-contained for external embedders.
 */
// Keep development panels and their font assets out of the production bundle.
const DeveloperTools = import.meta.env.DEV
  ? lazy(() => import("./DeveloperTools"))
  : null;

export const App: FC = () => (
  <QueryClientProvider client={queryClient}>
    <LogLocationGate>
      <AppConfigGate>
        <AppContent />
      </AppConfigGate>
    </LogLocationGate>
    {/* navigator.webdriver: skip devtools under Playwright — the floating
        button is an extra img/button that trips strict-mode locators. */}
    {DeveloperTools && !navigator.webdriver && (
      <Suspense fallback={null}>
        <DeveloperTools />
      </Suspense>
    )}
  </QueryClientProvider>
);
