/** The part of UI5's sap/ui/qunit/QUnitUtils the journeys use; @sapui5/types ships no declaration for it. */
declare module "sap/ui/qunit/QUnitUtils" {
    const QUnitUtils: {
        triggerKeydown(target: HTMLElement | string, key: string, shift?: boolean, alt?: boolean, ctrl?: boolean): void;
        triggerKeyup(target: HTMLElement | string, key: string, shift?: boolean, alt?: boolean, ctrl?: boolean): void;
    };
    export default QUnitUtils;
}
