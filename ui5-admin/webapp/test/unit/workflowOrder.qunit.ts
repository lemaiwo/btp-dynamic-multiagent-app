import { canMoveWithinGroup, groupSteps, swap } from "com/agent/admin/model/workflowOrder";
import { WorkflowBranch, WorkflowStep } from "com/agent/admin/service/types";

QUnit.module("workflowOrder");

function step(agent: string, branchKey: string | null): WorkflowStep {
    return {
        branch_key: branchKey,
        position: 0,
        agent_name: agent,
        instructions: "",
        fan_out: false,
        step_timeout_seconds: 300
    };
}

function branch(key: string, position: number): WorkflowBranch {
    return { key, description: "", position };
}

const names = (steps: WorkflowStep[]): string[] => steps.map((s) => s.agent_name);

QUnit.test("main-line steps come before every branch step", (assert) => {
    const steps = [step("analyst", "review"), step("fetcher", null), step("digest", null)];
    const grouped = groupSteps(steps, [branch("review", 1)]);

    assert.deepEqual(names(grouped), ["fetcher", "digest", "analyst"]);
});

QUnit.test("branch groups follow declared branch order", (assert) => {
    const steps = [step("escalator", "escalate"), step("fetcher", null), step("analyst", "review")];
    const branches = [branch("review", 1), branch("escalate", 2)];

    assert.deepEqual(names(groupSteps(steps, branches)), ["fetcher", "analyst", "escalator"]);
});

QUnit.test("relative order inside a group is preserved", (assert) => {
    const steps = [step("second", "review"), step("first", null), step("third", "review")];
    const grouped = groupSteps(steps, [branch("review", 1)]);

    assert.deepEqual(names(grouped), ["first", "second", "third"]);
});

QUnit.test("a branch only a step mentions sorts after the declared ones", (assert) => {
    const steps = [step("ghost", "undeclared"), step("analyst", "review"), step("fetcher", null)];
    const grouped = groupSteps(steps, [branch("review", 1)]);

    assert.deepEqual(names(grouped), ["fetcher", "analyst", "ghost"]);
});

QUnit.test("the first row of a group cannot move up", (assert) => {
    // Grouped order: fetcher(main), analyst(review), escalator(review).
    const steps = [step("fetcher", null), step("analyst", "review"), step("escalator", "review")];

    assert.notOk(canMoveWithinGroup(steps, 0, -1), "main line's first row");
    assert.notOk(canMoveWithinGroup(steps, 1, -1), "branch's first row, neighbour is main line");
    assert.ok(canMoveWithinGroup(steps, 2, -1), "second row of the branch");
});

QUnit.test("the last row of a group cannot move down", (assert) => {
    const steps = [step("fetcher", null), step("digest", null), step("analyst", "review")];

    assert.ok(canMoveWithinGroup(steps, 0, 1), "first of two main-line rows");
    assert.notOk(canMoveWithinGroup(steps, 1, 1), "last main-line row, neighbour is a branch");
    assert.notOk(canMoveWithinGroup(steps, 2, 1), "last row overall");
});

QUnit.test("swap exchanges neighbours without mutating the input", (assert) => {
    const steps = [step("a", null), step("b", null)];
    const moved = swap(steps, 1, -1);

    assert.deepEqual(names(moved), ["b", "a"], "returns the swapped order");
    assert.deepEqual(names(steps), ["a", "b"], "leaves the original array alone");
});

QUnit.test("swap outside the array is a no-op", (assert) => {
    const steps = [step("a", null), step("b", null)];

    assert.deepEqual(names(swap(steps, 0, -1)), ["a", "b"], "above the first row");
    assert.deepEqual(names(swap(steps, 1, 1)), ["a", "b"], "below the last row");
});
