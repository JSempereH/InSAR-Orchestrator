export interface PanelConfig {
  x: number;
  y: number;
  w: number;
  h: number;
  mode: "normal" | "min" | "max";
}

export const DEFAULT_PANEL: PanelConfig = { x: 16, y: 64, w: 520, h: 470, mode: "normal" };
