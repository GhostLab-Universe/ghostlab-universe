import type { TaskSpec } from "../api/api.generated.ts";

export interface TimelineTask {
  task: TaskSpec;
  /** Dependencies not present in this list. Shown rather than dropped. */
  unknownDependencies: string[];
}

/** Dependencies first, otherwise the order the service returned. Cycles keep that order. */
export function orderTasks(tasks: readonly TaskSpec[]): TimelineTask[] {
  const known = new Map(tasks.map((task) => [task.task_id, task]));
  const placed = new Set<string>();
  const visiting = new Set<string>();
  const ordered: TaskSpec[] = [];

  function place(task: TaskSpec): void {
    if (placed.has(task.task_id) || visiting.has(task.task_id)) return;
    visiting.add(task.task_id);
    for (const dependency of task.depends_on) {
      const before = known.get(dependency);
      if (before !== undefined) place(before);
    }
    visiting.delete(task.task_id);
    placed.add(task.task_id);
    ordered.push(task);
  }

  tasks.forEach(place);
  return ordered.map((task) => ({
    task,
    unknownDependencies: task.depends_on.filter((dependency) => !known.has(dependency)),
  }));
}
