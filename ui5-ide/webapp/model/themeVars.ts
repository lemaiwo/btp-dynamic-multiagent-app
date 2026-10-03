import Parameters from "sap/ui/core/theming/Parameters";
import Theming from "sap/ui/core/Theming";

/**
 * Theme parameters the editor's own CSS (diff table, rendered markdown)
 * reads as `var(--name)`. UI5 1.120 publishes theme parameters as CSS
 * custom properties only behind an experimental flag, so they are copied
 * onto :root here, and again whenever the theme changes. Colours thereby
 * follow the active theme (Horizon, dark, high contrast) instead of being
 * hard-coded.
 */
const NAMES = [
    "sapTextColor", "sapContent_LabelColor", "sapFontFamily", "sapFontSmallSize",
    "sapContent_MonospaceFontFamily", "sapList_HeaderBackground", "sapList_BorderColor",
    "sapGroup_ContentBackground", "sapGroup_ContentBorderColor",
    "sapSuccessBackground", "sapErrorBackground", "sapWarningBackground", "sapErrorBorderColor",
    "sapPositiveElementColor", "sapNegativeElementColor", "sapCriticalElementColor"
];

let attached = false;

function copy(values: Record<string, string> | string | undefined): void {
    if (!values || typeof values === "string") {
        return;
    }
    const style = document.documentElement.style;
    Object.entries(values).forEach(([name, value]) => {
        if (value) {
            style.setProperty(`--${name}`, value);
        }
    });
}

function read(): void {
    const sync = Parameters.get({ name: NAMES, callback: copy }) as Record<string, string> | string | undefined;
    copy(sync);
}

/** Copies the parameters now and on every later theme change; idempotent. */
export function applyThemeVars(): void {
    if (!attached) {
        attached = true;
        Theming.attachApplied(read);
    }
    read();
}
