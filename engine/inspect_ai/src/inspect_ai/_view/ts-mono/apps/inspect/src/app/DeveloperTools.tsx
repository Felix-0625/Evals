import { TanStackDevtools } from "@tanstack/react-devtools";
import { ReactQueryDevtoolsPanel } from "@tanstack/react-query-devtools";
import { FC } from "react";

import { ZustandDevtoolsPanel } from "@tsmono/zustand-devtools";

import { storeImplementation } from "../state/store";

// The store is created during app initialization, after this module may load.
const ZustandStorePanel: FC<{ theme: "light" | "dark" }> = ({ theme }) =>
  storeImplementation ? (
    <ZustandDevtoolsPanel store={storeImplementation} theme={theme} />
  ) : (
    <div>Store not initialized</div>
  );

export default function DeveloperTools() {
  return (
    <TanStackDevtools
      plugins={[
        { name: "TanStack Query", render: <ReactQueryDevtoolsPanel /> },
        {
          name: "Zustand Inspector",
          render: (_el, props) => <ZustandStorePanel theme={props.theme} />,
        },
      ]}
    />
  );
}
