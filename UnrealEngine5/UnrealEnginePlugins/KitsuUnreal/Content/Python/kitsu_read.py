import gazu
import unreal

def read_comments(project_name: str, sequence_name: str, shot_name: str, task_type_name: str):

    project = gazu.project.get_project_by_name(project_name)
    if not project:
        unreal.log_error(f"❌ No project named {project_name} found in Kitsu")
        return [], None, None
    sequence = gazu.shot.get_sequence_by_name(project, sequence_name)
    if not sequence:
        unreal.log_error(f"❌ No sequence named {sequence_name} found in any open project")
        return [], None, None
    shot = gazu.shot.get_shot_by_name(sequence, shot_name)
    if not shot:
        unreal.log_error(f"❌ No shot named {shot_name} in Kitsu")
        return [], None, None
    task_type = gazu.task.get_task_type_by_name(task_type_name)
    if not task_type:
        unreal.log_error(f"❌ Task type '{task_type_name}' not found in Kitsu")
        return [], None, None
    task = gazu.task.get_task_by_entity(shot, task_type)
    if not task:
        unreal.log_error(f"❌ No '{task_type_name}' task found for shot {shot_name}")
        return [], None, None

    comments = gazu.task.all_comments_for_task(task) or []
    comment_texts = [comment["text"] for comment in comments if comment["text"]]

    description = shot.get("description") or " "
    data = shot.get("data") or {}
    animation = data.get("animation", "")

    return comment_texts, description, animation


# Run as a script by an "Execute Python Script" node that defines these names as inputs.
# Imported as a module, they don't exist: only define read_comments.
if all(name in globals() for name in ("project_name", "sequence_name", "shot_name", "task_type_name")):
    comment_texts, description, animation = read_comments(project_name, sequence_name, shot_name, task_type_name)
