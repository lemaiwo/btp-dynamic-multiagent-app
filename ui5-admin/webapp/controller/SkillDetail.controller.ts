import JSONModel from "sap/ui/model/json/JSONModel";
import MessageToast from "sap/m/MessageToast";
import { ValueState } from "sap/ui/core/library";
import BaseController from "./BaseController";
import ErrorHandler from "../service/ErrorHandler";
import { AdminError } from "../service/AdminService";
import type { Route$PatternMatchedEvent } from "sap/ui/core/routing/Route";
import type { SkillInput } from "../service/types";

const EMPTY: SkillInput = { name: "", description: "", content: "" };

/**
 * @namespace com.infrabel.agentadmin.controller
 */
export default class SkillDetail extends BaseController {

    private skillId?: number;

    public onInit(): void {
        this.setModel(new JSONModel({ title: "", data: { ...EMPTY }, errors: {} }), "skill");
        this.getRouter().getRoute("skillDetail")?.attachPatternMatched((event: Route$PatternMatchedEvent) => {
            const id = (event.getParameter("arguments") as { skillId: string }).skillId;
            void this.load(id);
        });
    }

    private async load(id: string): Promise<void> {
        const model = this.getModel("skill") as JSONModel;
        model.setProperty("/errors", {});

        if (id === "new") {
            this.skillId = undefined;
            model.setProperty("/data", { ...EMPTY });
            model.setProperty("/title", this.text("newSkill"));
            return;
        }

        this.skillId = Number(id);
        const skill = await this.run(
            this.getAdminService().getSkill(this.skillId),
            "Could not load the skill."
        );
        if (skill) {
            model.setProperty("/data", {
                name: skill.name,
                description: skill.description,
                content: skill.content
            });
            model.setProperty("/title", skill.name);
        }
    }

    public async onSave(): Promise<void> {
        const model = this.getModel("skill") as JSONModel;
        model.setProperty("/errors", {});
        const data = model.getProperty("/data") as SkillInput;

        try {
            const saved = await this.getAdminService().upsertSkill(data, this.skillId);
            MessageToast.show(this.text("skillSaved"));
            this.skillId = saved.id;
            model.setProperty("/title", saved.name);
            this.getRouter().navTo("skills");
        } catch (error) {
            if (error instanceof AdminError && Object.keys(error.fieldErrors).length > 0) {
                this.applyFieldErrors(error.fieldErrors);
                return;
            }
            ErrorHandler.handle(error, "Could not save the skill.");
        }
    }

    /** Attaches server-side 422 messages to the matching form controls. */
    private applyFieldErrors(fieldErrors: Record<string, string>): void {
        const model = this.getModel("skill") as JSONModel;
        const errors: Record<string, string> = {};
        ["name", "description", "content"].forEach((field) => {
            const message = fieldErrors[field];
            if (message) {
                errors[field] = message;
                errors[`${field}State`] = ValueState.Error;
            }
        });
        model.setProperty("/errors", errors);
    }

    public onBack(): void {
        this.getRouter().navTo("skills");
    }

    private text(key: string): string {
        const bundle = this.getOwnerComponentTyped().getModel("i18n") as unknown as {
            getResourceBundle(): { getText(k: string): string };
        };
        return bundle.getResourceBundle().getText(key);
    }
}
