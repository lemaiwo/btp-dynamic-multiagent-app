import JSONModel from "sap/ui/model/json/JSONModel";
import MessageBox from "sap/m/MessageBox";
import MessageToast from "sap/m/MessageToast";
import BaseController from "./BaseController";
import type Event from "sap/ui/base/Event";
import type ColumnListItem from "sap/m/ColumnListItem";
import type Control from "sap/ui/core/Control";
import type { Skill } from "../service/types";

/**
 * @namespace com.infrabel.agentadmin.controller
 */
export default class Skills extends BaseController {

    public onInit(): void {
        this.setModel(new JSONModel({ items: [] }), "skills");
        this.getRouter().getRoute("skills")?.attachPatternMatched(() => {
            void this.load();
        });
    }

    private async load(): Promise<void> {
        const skills = await this.run(
            this.getAdminService().listSkills(),
            "Could not load the skills."
        );
        if (skills) {
            (this.getModel("skills") as JSONModel).setProperty("/items", skills);
        }
    }

    public onCreate(): void {
        this.getRouter().navTo("skillDetail", { skillId: "new" });
    }

    public onOpen(event: Event): void {
        const skill = (event.getSource() as ColumnListItem)
            .getBindingContext("skills")?.getObject() as Skill;
        this.getRouter().navTo("skillDetail", { skillId: String(skill.id) });
    }

    public onDelete(event: Event): void {
        const skill = (event.getSource() as Control)
            .getBindingContext("skills")?.getObject() as Skill;

        MessageBox.confirm(this.text("deleteSkillConfirm").replace("{0}", skill.name), {
            title: this.text("delete"),
            emphasizedAction: MessageBox.Action.OK,
            onClose: (action: string) => {
                if (action === MessageBox.Action.OK) {
                    void this.doDelete(skill);
                }
            }
        });
    }

    private async doDelete(skill: Skill): Promise<void> {
        const ok = await this.runOk(
            this.getAdminService().deleteSkill(skill.id),
            `Could not delete the skill "${skill.name}".`
        );
        if (ok) {
            MessageToast.show(this.text("skillDeleted"));
            void this.load();
        }
    }

    private text(key: string): string {
        const bundle = this.getOwnerComponentTyped().getModel("i18n") as unknown as {
            getResourceBundle(): { getText(k: string): string };
        };
        return bundle.getResourceBundle().getText(key);
    }
}
