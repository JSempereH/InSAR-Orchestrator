import { StorageTarget } from "../api/client";

interface StorageTargetSelectProps {
  targets: StorageTarget[] | undefined;
  value: string;
  onChange: (mountpoint: string) => void;
  /** Include "of {total_gb} GB" in each option's label (NewProjectWizard
   * shows it, GroundMotion doesn't - preserved as-is from before this was
   * shared, not a meaningful difference worth forcing to match). */
  showTotal?: boolean;
  className?: string;
}

/**
 * The "which disk should this be saved to" dropdown - was duplicated
 * verbatim between NewProjectWizard and GroundMotion.
 */
export function StorageTargetSelect({ targets, value, onChange, showTotal = false, className = "input" }: StorageTargetSelectProps) {
  return (
    <select className={className} value={value} onChange={(e) => onChange(e.target.value)}>
      {targets?.map((t) => (
        <option key={t.mountpoint ?? "default"} value={t.mountpoint ?? ""} disabled={!t.writable}>
          {t.mountpoint ? `${t.mountpoint} (${t.device})` : "App default"}
          {" · "}{t.free_gb} GB free{showTotal ? ` of ${t.total_gb} GB` : ""}{!t.writable ? " · not writable" : ""}
        </option>
      ))}
    </select>
  );
}
