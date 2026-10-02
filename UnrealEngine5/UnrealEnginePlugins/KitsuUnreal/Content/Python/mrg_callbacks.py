import unreal
import kitsu_utils
import os
import time
import datetime
from concurrent.futures import ThreadPoolExecutor

MOVIE_EXTENSIONS = ('.mp4', '.mov')

# Render start time per job name, set in on_job_start
_job_start_times = {}
# Sequence frame range per job name, read in on_job_start: during the render, MRG has
# replaced the sequence's playback range with the graph's custom range, if any
_job_frame_ranges = {}

# Proxy encode + upload run here, one at a time, so the editor stays usable
_publisher = ThreadPoolExecutor(max_workers=1, thread_name_prefix="KitsuUnreal")
_pending = []

# How long editor shutdown waits for publishes still running
SHUTDOWN_WAIT_SECONDS = 15 * 60


def is_unattended():
    """
    True on the farm. Deadline launches Unreal with -unattended (and sets
    DEADLINE_RPC_PORT in editor mode) and may quit it as soon as the render is
    done, which would kill a background publish: publish synchronously there.
    """
    command_line = unreal.SystemLibrary.get_command_line().lower()
    return "-unattended" in command_line or "DEADLINE_RPC_PORT" in os.environ


def _wait_for_pending():
    running = [f for f in _pending if not f.done()]
    if not running:
        return
    unreal.log_warning(f"🦊 Waiting for {len(running)} Kitsu publish(es) to finish before closing...")
    deadline = time.time() + SHUTDOWN_WAIT_SECONDS
    for future in running:
        try:
            future.result(timeout=max(0, deadline - time.time()))
        except Exception as e:
            unreal.log_error(f"🦊 ❌ Kitsu publish not finished at shutdown: {e!r}")

unreal.register_python_shutdown_callback(_wait_for_pending)


def publish(infos, all_files, render_time_str, frame_rate=None, frame_range=None):
    """
    Makes the file to publish and uploads it. Exactly one file per render:
    - EXR output: one mp4 proxy made by daily (the whole sequence if its frames are on disk), exact frame count checked
    - movie output: the first movie
    - anything else: only if it's a single file (a still)

    No Unreal API use besides logging: runs on a worker thread in the editor.
    """
    project_name, sequence_name, shot_name, task_type, task_status, comment = infos

    exr_files = [f for f in all_files if f.lower().endswith('.exr')]
    movie_files = [f for f in all_files if f.lower().endswith(MOVIE_EXTENSIONS)]
    upload_file = None
    daily_info = {}

    if exr_files:
        unreal.log(f"🎞️ EXR output detected — running daily to generate proxy...")
        upload_file, daily_info = kitsu_utils.generate_proxy(exr_files, frame_rate, frame_range)
        if upload_file:
            unreal.log(f"✅ Proxy generated: {upload_file}")
        else:
            unreal.log_error("⚠️ No complete proxy, won't upload to kitsu.")
            return False
    elif movie_files:
        upload_file = movie_files[0]
        if len(movie_files) > 1:
            unreal.log_warning(f"⚠️ {len(movie_files)} movies rendered, only uploading {upload_file}")
    elif len(all_files) == 1:
        upload_file = all_files[0]
    elif all_files:
        unreal.log_warning(f"⚠️ {len(all_files)} non-EXR image files rendered: only EXR sequences get a proxy, nothing uploaded")
        return False

    if not upload_file:
        return False

    daily_info["render_time"] = render_time_str
    return kitsu_utils.upload_shots(project_name, sequence_name, shot_name, task_type, task_status, comment, upload_file, daily_info)


def _publish_logged(*args):
    # Exceptions in a worker thread are otherwise lost
    try:
        return publish(*args)
    except Exception as e:
        unreal.log_error(f"🦊 ❌ Kitsu publish failed: {e!r}")
        return False


@unreal.uclass()
class MRG_Kitsu_Callback(unreal.MovieGraphScriptBase):

    @unreal.ufunction(override=True)
    def on_job_start(self, in_job_copy):
        super().on_job_start(in_job_copy)
        _job_start_times[in_job_copy.job_name] = time.time()
        _job_frame_ranges[in_job_copy.job_name] = kitsu_utils.get_sequence_frame_range(in_job_copy)
        unreal.log("▶️ Job is starting: {}".format(in_job_copy.job_name))

    @unreal.ufunction(override=True)
    def on_job_finished(self, in_job_copy, in_output_data):
        super().on_job_finished(in_job_copy, in_output_data)

        # Log time to complete renderJob
        start_time = _job_start_times.pop(in_job_copy.job_name, None)
        frame_range = _job_frame_ranges.pop(in_job_copy.job_name, None)
        render_seconds = time.time() - start_time if start_time else 0
        render_time_str = str(datetime.timedelta(seconds=int(render_seconds)))
        unreal.log(f"⏱️ Render time: {render_time_str}")

        # Cancelled or failed render: partial output, never publish it
        if not getattr(in_output_data, "success", True):
            unreal.log_warning("⚠️ Render did not complete successfully, nothing uploaded to Kitsu")
            return

        # Everything that touches Unreal objects happens here, on the game thread
        infos = kitsu_utils.get_unreal_infos(in_job_copy)
        frame_rate = kitsu_utils.get_render_frame_rate(in_job_copy)
        project_name, sequence_name, shot_name, task_type, task_status, comment = infos
        if not (project_name and sequence_name and shot_name and task_type and task_status):
            unreal.log_error("❌ Kitsu project, sequence, shot, task type or status unresolved, nothing uploaded (see log above)")
            return

        # Gather output files
        all_files = []
        for shot in in_output_data.graph_data:
            for layer_id, layer_data in shot.render_layer_data.items():
                all_files.extend(str(f) for f in layer_data.file_paths)

        unreal.log(f"📝 Found {len(all_files)} output files")

        if is_unattended():
            _publish_logged(infos, all_files, render_time_str, frame_rate, frame_range)
        else:
            unreal.log("🦊 Publishing to Kitsu in the background, see the log for the result")
            _pending[:] = [f for f in _pending if not f.done()]
            _pending.append(_publisher.submit(_publish_logged, infos, all_files, render_time_str, frame_rate, frame_range))
