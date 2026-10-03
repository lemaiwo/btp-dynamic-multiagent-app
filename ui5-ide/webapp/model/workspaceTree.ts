import type { FileState, FileSummary } from "../service/types";

/**
 * The object types the open-object dialog offers: exactly those the
 * workspace path contract maps to an abapGit file (plan §1.4).
 */
export const OBJECT_TYPES = ["CLAS", "INTF", "PROG", "DDLS", "BDEF", "DCLS", "DDLX", "SRVD", "FUNC"] as const;

/** One node of the explorer's workspace tree (a JSONModel tree, children in `nodes`). */
export interface TreeNode {
    /** The last path segment, shown in the tree. */
    text: string;
    /** The full workspace path (`src/CLAS` for a folder, the file path for a leaf). */
    path: string;
    folder: boolean;
    /** The file's state; `null` on a folder. */
    state: FileState | null;
    objectType: string | null;
    objectName: string | null;
    nodes: TreeNode[];
}

function folderNode(text: string, path: string): TreeNode {
    return { text, path, folder: true, state: null, objectType: null, objectName: null, nodes: [] };
}

function sortTree(nodes: TreeNode[]): TreeNode[] {
    nodes.sort((a, b) => {
        if (a.folder !== b.folder) {
            return a.folder ? -1 : 1;
        }
        return a.text.localeCompare(b.text);
    });
    nodes.forEach((n) => sortTree(n.nodes));
    return nodes;
}

/**
 * Turns the flat workspace file list into a folder tree by path segment:
 * `src/CLAS/zcl_x.clas.abap` becomes `src` > `CLAS` > `zcl_x.clas.abap`.
 * Folders come before files at every level, each group sorted by name.
 */
export function buildTree(files: FileSummary[]): TreeNode[] {
    const roots: TreeNode[] = [];
    const folders = new Map<string, TreeNode>();

    for (const file of files) {
        const segments = file.path.split("/").filter(Boolean);
        let siblings = roots;
        for (let i = 0; i < segments.length - 1; i++) {
            const path = segments.slice(0, i + 1).join("/");
            let folder = folders.get(path);
            if (!folder) {
                folder = folderNode(segments[i], path);
                folders.set(path, folder);
                siblings.push(folder);
            }
            siblings = folder.nodes;
        }
        siblings.push({
            text: segments[segments.length - 1] ?? file.path,
            path: file.path,
            folder: false,
            state: file.state,
            objectType: file.object_type,
            objectName: file.object_name,
            nodes: []
        });
    }
    return sortTree(roots);
}
