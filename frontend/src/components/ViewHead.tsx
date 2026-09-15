import type { ReactNode } from "react";

import { Kicker } from "./Kicker";

interface ViewHeadProps {
  kicker: ReactNode;
  title: ReactNode;
  meta?: ReactNode;
  /** One short paragraph under the title: what the page is for. */
  description?: ReactNode;
}

export function ViewHead({ kicker, title, meta, description }: ViewHeadProps) {
  return (
    <div className="vhead">
      <Kicker>{kicker}</Kicker>
      <h1>{title}</h1>
      {meta != null && <span className="meta">{meta}</span>}
      {description != null && <p className="vhead-desc">{description}</p>}
    </div>
  );
}
