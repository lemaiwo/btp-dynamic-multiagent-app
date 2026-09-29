import SimpleType from "sap/ui/model/SimpleType";

/**
 * The key the "Main line" option carries in a step's branch dropdown.
 *
 * It cannot be `""`: `sap.m.Select` reads an empty `selectedKey` as "no
 * selection" and shows a blank control even when an item with that key
 * exists. It cannot be `null` either, because item keys are strings. So the
 * option gets this sentinel, and {@link NullableKey} translates it to the
 * `null` the model, the flow preview and the ordering helpers use.
 */
export const MAIN_LINE_KEY = "__main__";

/**
 * Two-way binding type between a step's `branch_key` (`null` for the main
 * line) and the branch `<Select>` (which needs a non-empty key per item).
 *
 * Bound directly, the two never match: the control shows blank for
 * main-line steps, and choosing "Main line" writes `""` into the model,
 * which nothing downstream treats as main line until the row is saved.
 * This type maps `null` to {@link MAIN_LINE_KEY} on the way to the control
 * and the sentinel (or an empty key) back to `null` on the way to the model.
 */
export default class NullableKey extends SimpleType {
    public formatValue(value: unknown): string {
        const text = value === null || value === undefined ? "" : String(value).trim();
        return text === "" ? MAIN_LINE_KEY : text;
    }

    public parseValue(value: unknown): string | null {
        const text = value === null || value === undefined ? "" : String(value).trim();
        return text === "" || text === MAIN_LINE_KEY ? null : text;
    }

    public validateValue(): void {
        // Every string is a valid key; the server validates the branch itself.
    }
}
