import unreal
import gazu
import os
import time
import configparser
import logging
import socket
import shutil
import subprocess

TASK_TYPE_ENUM_PATH = "/KitsuUnreal/Enums/KitsuTaskType.KitsuTaskType"
TASK_STATUS_ENUM_PATH = "/KitsuUnreal/Enums/KitsuTaskStatus.KitsuTaskStatus"
KISMET_NODE_HELPER_CDO = "/Script/Engine.Default__KismetNodeHelperLibrary"

def get_unreal_infos(job):
    # Extracts project, sequence, shot, task type, and comment from a Movie Render Queue job.
    
    # Access asset used from the job
    source_asset_softpath = job.get_editor_property("sequence")
    Source_asset_softobject = unreal.SystemLibrary.conv_soft_obj_path_to_soft_obj_ref(source_asset_softpath)
    source_asset_path = unreal.SystemLibrary.conv_soft_object_reference_to_string(Source_asset_softobject)
    source_asset = unreal.load_asset(source_asset_path)

    comment = job.get_editor_property("comment")

    if not source_asset:
        unreal.log_error(f"❌ Asset failed to load from path: {source_asset_path}")
        return None, None, None, None, None, comment
    shot_name = unreal.SystemLibrary.get_display_name(source_asset)

    # Get data from the CineAssembly metadata if possible
    if hasattr(unreal, 'CineAssembly') and isinstance(source_asset, unreal.CineAssembly):
        unreal.log(f"🎬 CineAssembly asset: {shot_name}")

        sequence_name = source_asset.get_metadata_as_string("Sequence")
        unreal.log(f"🎬 Sequence name: {sequence_name}")

        # Get the project name from the metadata production
        project_name = source_asset.get_production_name()
        unreal.log(f"🎬 Project name: {project_name}")
    else:
        # If this is a sequence !
        unreal.log_warning("🎬 Sequence is not an Assembly; falling back to parsing infos")
        unreal.log(f"🎬 Shot name: {shot_name}")

        # Fallback: extract between first and last underscore
        parts = shot_name.split("_")
        sequence_name = parts[1] if len(parts) >= 3 else None
        unreal.log(f"🎬 Sequence name: {sequence_name}")

        # A plain level sequence carries no production: there is no project to publish to
        project_name = None
        unreal.log_error("❌ No Kitsu project for a plain Level Sequence: render a CineAssembly with a production set")


    # Task type and status come from the exposed enum variables of the graph.
    # A sequence-level task type (e.g. an edit of the whole sequence) is resolved at upload.
    # A missing variable falls back to the default; a value that can't be resolved
    # returns None so the caller skips the upload instead of publishing to the wrong task.
    graph_config = job.get_graph_preset()

    task_type = get_enum_variable_name(job, graph_config, "KitsuTaskType", TASK_TYPE_ENUM_PATH, "Layout")
    unreal.log(f"🎬 KitsuTaskType = {task_type}")

    task_status = get_enum_variable_name(job, graph_config, "KitsuTaskStatus", TASK_STATUS_ENUM_PATH, "wip")
    if task_status:
        # Enum display names are "WIP", "WFA"...; Kitsu short names are lowercase
        task_status = task_status.lower()
    unreal.log(f"🎬 KitsuTaskStatus = {task_status}")


    if comment:
        unreal.log(f"🎬 Comment: {comment}")


    return project_name, sequence_name, shot_name, task_type, task_status, comment


def format_frame_rate(frame_rate):
    """unreal.FrameRate -> "25", "24", "29.97"..."""
    fps = frame_rate.numerator / frame_rate.denominator
    return str(int(fps)) if fps.is_integer() else f"{fps:g}"


def get_render_frame_rate(job):
    """
    Frame rate the job renders at: the graph's Global Output Settings frame rate if
    overridden, else the sequence's display rate. Game thread only (Unreal API).

    Not the unreal/frameRate EXR header: UE 5.7 writes 24 there unless the graph
    overrides the frame rate (FMovieGraphFilenameResolveParams::MakeResolveParams
    never sets DefaultFrameRate, which defaults to 24).
    """
    try:
        graph = job.get_graph_preset()
        if graph:
            for node in graph.get_nodes_for_branch(unreal.MovieGraphGlobalOutputSettingNode, "Globals", False):
                if node.get_editor_property("override_output_frame_rate"):
                    return format_frame_rate(node.get_editor_property("output_frame_rate"))
        # Same resolution of the job's sequence as get_unreal_infos
        soft_ref = unreal.SystemLibrary.conv_soft_obj_path_to_soft_obj_ref(job.get_editor_property("sequence"))
        sequence = unreal.load_asset(unreal.SystemLibrary.conv_soft_object_reference_to_string(soft_ref))
        if sequence:
            return format_frame_rate(sequence.get_display_rate())
    except Exception as e:
        unreal.log_warning(f"⚠️ Could not get the render frame rate from the job: {e}")
    return None


def get_sequence_frame_range(job):
    """
    (first, last) output frame numbers of the job's whole sequence, as MRG writes them
    in {frame_number}: the playback range at the output frame rate, plus handles and the
    frame number offset of the graph's Global Output Settings. A custom playback range
    is ignored on purpose: the proxy covers the whole sequence when its frames are on disk.
    Returns None if it can't be worked out. Game thread only (Unreal API).
    """
    try:
        soft_ref = unreal.SystemLibrary.conv_soft_obj_path_to_soft_obj_ref(job.get_editor_property("sequence"))
        sequence = unreal.load_asset(unreal.SystemLibrary.conv_soft_object_reference_to_string(soft_ref))
        if not sequence:
            return None
        display_rate = sequence.get_display_rate()
        output_rate, handles, offset = display_rate, 0, 0
        graph = job.get_graph_preset()
        if graph:
            for node in graph.get_nodes_for_branch(unreal.MovieGraphGlobalOutputSettingNode, "Globals", False):
                if node.get_editor_property("override_output_frame_rate"):
                    output_rate = node.get_editor_property("output_frame_rate")
                if node.get_editor_property("override_handle_frame_count"):
                    handles = node.get_editor_property("handle_frame_count")
                if node.get_editor_property("override_frame_number_offset"):
                    offset = node.get_editor_property("frame_number_offset")

        def to_output(display_frame):
            # display frames -> output frames, floored like Unreal's FFrameRate::TransformTime
            num = display_frame * output_rate.numerator * display_rate.denominator
            return num // (output_rate.denominator * display_rate.numerator)

        start, end = sequence.get_playback_start(), sequence.get_playback_end()  # end exclusive
        if end <= start:
            return None
        first, last = to_output(start) - handles + offset, to_output(end) - 1 + handles + offset
        unreal.log(f"🎬 Sequence frames {first}-{last} (playback {start}-{end} at {format_frame_rate(display_rate)} fps)")
        return first, last
    except Exception as e:
        unreal.log_warning(f"⚠️ Could not get the sequence frame range from the job: {e}")
    return None


def get_variable_from_graph(job, graph_config, variable_name):
    """
    Search the top-level graph and all contained subgraphs recursively
    for a variable by name. Returns (variable, overrides) for the first match,
    prioritizing the top-level graph.
    """
    # Check top-level graph first — job overrides are most likely keyed here
    var = graph_config.get_variable_by_name(variable_name)
    if var:
        overrides = job.get_or_create_variable_overrides(graph_config)
        unreal.log(f"🎬 Found '{variable_name}' in top-level graph")
        return var, overrides

    # Walk all subgraphs recursively (API handles the recursion for us)
    subgraphs = graph_config.get_all_contained_subgraphs()
    for subgraph in subgraphs:
        var = subgraph.get_variable_by_name(variable_name)
        if var:
            # Try subgraph-keyed overrides first
            overrides = job.get_or_create_variable_overrides(subgraph)
            unreal.log(f"🎬 Found '{variable_name}' in subgraph: {subgraph.get_name()}")
            return var, overrides

    unreal.log_warning(f"⚠️ Variable '{variable_name}' not found in graph or any subgraph")
    return None, None


def get_enum_display_name(enum, value):
    """
    Display name of an enum value, as set in the Unreal enum asset.
    Returns None if the value is not a valid entry of the enum, or if it can't be read.

    UE 5.7 doesn't expose KismetNodeHelperLibrary to Python (its functions are
    BlueprintInternalUseOnly, what the "Enum to String" node calls) and DisplayNameMap
    is protected, but call_method on the library's default object reaches its functions
    by name. Game thread only.
    """
    try:
        helper = unreal.load_object(None, KISMET_NODE_HELPER_CDO)
        name = str(helper.call_method("GetEnumeratorName", (enum, value)))
        # "None" past the last entry, "<Enum>::<Enum>_MAX" for the implicit last one
        if not name or name == "None" or name.endswith("_MAX"):
            return None
        return str(helper.call_method("GetEnumeratorUserFriendlyName", (enum, value))) or None
    except Exception as e:
        unreal.log_warning(f"⚠️ Could not read the display name of {enum.get_name()} value {value}: {e}")
        return None


def get_enum_variable_name(job, graph_config, variable_name, enum_path, default):
    """
    Resolves an enum graph variable (e.g. KitsuTaskType) to the display name of its value.

    Returns `default` if the enum asset or the variable is missing from the graph,
    and None if the variable is there but its value can't be resolved.
    """
    enum = unreal.load_object(None, enum_path)
    if not enum:
        unreal.log_warning(f"❌ Enum asset '{enum_path}' not found, fallback to '{default}'")
        return default

    var, overrides = get_variable_from_graph(job, graph_config, variable_name)
    if not (var and overrides):
        unreal.log_warning(f"⚠️ Variable '{variable_name}' not found on graph config, fallback to '{default}'")
        return default

    value = overrides.get_value_enum(var, enum)
    if value is None:
        unreal.log_error(f"❌ Could not resolve {variable_name} enum value")
        return None
    value = int(value)

    name = get_enum_display_name(enum, value)
    if not name:
        unreal.log_error(f"❌ {variable_name} value {value} has no display name in {enum.get_name()}")
    return name


def connect_kitsu(attempts=3, retry_delay=5):
    """
    Logs in to Kitsu with the plugin's credentials (personal account first, then bot token).
    Retries on network errors. Returns True when connected.
    """
    global _logged_in_as_user
    try:
        host, user, password, token = get_plugin_ini()
    except FileNotFoundError as e:
        unreal.log_error(f"🦊 {e}")
        return False

    gazu.set_host(host)
    for attempt in range(1, attempts + 1):
        try:
            if user and password:
                success = gazu.log_in(user, password)
                _logged_in_as_user = True
                unreal.log(f"🦊 KitsuUnreal connected with {success['user']['email']}")
            else:
                gazu.set_token(token)
                gazu.project.all_open_projects()
                unreal.log("🦊 KitsuUnreal connected via bot token")
            return True
        except Exception as e:
            unreal.log_warning(f"🦊 KitsuUnreal connection attempt {attempt}/{attempts} failed: {e}")
            if attempt < attempts:
                time.sleep(retry_delay)

    unreal.log_error("🦊 KitsuUnreal could not connect to Kitsu (wrong credentials, expired token or host unreachable)")
    return False


def ensure_connected():
    """Checks the Kitsu session is alive and reconnects if not. Returns True when connected."""
    try:
        gazu.project.all_open_projects()
        return True
    except Exception:
        unreal.log_warning("🦊 Kitsu session lost or never opened, reconnecting...")
        return connect_kitsu()


def disconnect_kitsu():
    # Only a user login has a session to close; a bot token has nothing to log out of
    if not _logged_in_as_user:
        return
    try:
        gazu.log_out()
        unreal.log("🦊 KitsuUnreal disconnected from Kitsu")
    except Exception as e:
        unreal.log_warning(f"🦊 KitsuUnreal logout failed: {e}")


_logged_in_as_user = False


def get_plugin_ini():

    """
    Reads the plugin's Config/KitsuID.ini (personal account),
    or Config/KitsuBot.ini (bot token) as a fallback.

    :returns: (host, user, password, token), user/password or token being None
    """
    # <plugin>/Content/Python/kitsu_utils.py -> <plugin>/Config. No Unreal API here:
    # this runs on the connection and publish threads, where Unreal refuses API calls.
    plugin_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    config_dir = os.path.join(plugin_root, "Config")
    
    user_ini = os.path.join(config_dir, "KitsuID.ini")
    bot_ini = os.path.join(config_dir, "KitsuBot.ini")

    # Try personal credentials first
    if os.path.exists(user_ini):
        config = configparser.ConfigParser()
        config.read(user_ini)
        if 'Kitsu' in config:
            host = config['Kitsu'].get('Host')
            user = config['Kitsu'].get('User')
            password = config['Kitsu'].get('Password')
            if host and user and password:
                unreal.log("🦊 Using personal Kitsu credentials")
                return host, user, password, None

    # Fall back to bot token
    if os.path.exists(bot_ini):
        config = configparser.ConfigParser()
        config.read(bot_ini)
        if 'Kitsu' in config:
            host = config['Kitsu'].get('Host')
            token = config['Kitsu'].get('Token')
            if host and token:
                unreal.log("🦊 No personal credentials found, using bot token")
                return host, None, None, token

    raise FileNotFoundError(
        "No Kitsu credentials found. Either create Config/KitsuID.ini with your personal credentials, or ensure Config/KitsuBot.ini is synced from source control."
    )


def _find_by_name(entities, name, kind):
    """Name match ignoring surrounding whitespace and case, for names typed with a stray space in Kitsu."""
    wanted = (name or "").strip().lower()
    for entity in entities or []:
        if entity["name"].strip().lower() == wanted:
            unreal.log_warning(f"⚠️ Kitsu {kind} is named {entity['name']!r}, not {name!r}: fix the name in Kitsu")
            return entity
    return None


def upload_shots(project_name: str, sequence_name: str, shot_name: str, task_type_name: str, task_status_name : str, comment: str, file_path: str, daily_info=None):
    """Publishes one file as a preview on the Kitsu task. One render = one preview."""

    if not task_type_name or not task_status_name:
        unreal.log_error(f"❌ Task type ({task_type_name}) or status ({task_status_name}) unresolved, nothing uploaded")
        return False

    # Build automated comment additions
    auto_comment_parts = []
    
    # Add hostname
    hostname = socket.gethostname()
    auto_comment_parts.append(f"Host: {hostname}")

    if daily_info:
        transform = daily_info.get("ocio_transform")
        if transform and len(transform) == 2:
            auto_comment_parts.append(f"Color: {transform[0]} → {transform[1]}")

        render_time = daily_info.get("render_time")
        if render_time:
            auto_comment_parts.append(f"Render time: {render_time}")
        # codec = daily_info.get("codec")
        # if codec:
        #     auto_comment_parts.append(f"Codec: {codec}")

        # res = daily_info.get("resolution")
        # if res and res[0] and res[1]:
        #     auto_comment_parts.append(f"Resolution: {res[0]}x{res[1]}")

    # Combine user comment with auto info
    full_comment = comment or ""
    if auto_comment_parts:
        auto_str = "\n\n".join(auto_comment_parts)
        full_comment = f"{full_comment}\n\n{auto_str}" if full_comment else f"{auto_str}"

    if not ensure_connected():
        unreal.log_error(f"🦊 ❌ Not connected to Kitsu, nothing uploaded for {shot_name}")
        return False

    try:
        # Get the project dict in Kitsu
        project = gazu.project.get_project_by_name(project_name) or _find_by_name(
            gazu.project.all_open_projects(), project_name, "project")
        if not project:
            unreal.log_error(f"❌ No project named {project_name} found in Kitsu")
            return False
        
        # Get the sequence dict in Kitsu
        sequence = gazu.shot.get_sequence_by_name(project, sequence_name) or _find_by_name(
            gazu.shot.all_sequences_for_project(project), sequence_name, "sequence")
        if not sequence:
            unreal.log_error(f"❌ No sequence named {sequence_name} found in any open project")
            return False
        
        # Get the shot in Kitsu
        shot = gazu.shot.get_shot_by_name(sequence, shot_name) or _find_by_name(
            gazu.shot.all_shots_for_sequence(sequence), shot_name, "shot")

        task_type = gazu.task.get_task_type_by_name(task_type_name)
        if not task_type:
            unreal.log_error(f"❌ Task type '{task_type_name}' not found in Kitsu")
            return False

        # The shot's task, else the sequence's: a sequence-level task type (e.g. an edit of
        # the whole sequence) has no task on shots, and a master sequence has no shot
        task = gazu.task.get_task_by_entity(shot, task_type) if shot else None
        if not task:
            task = gazu.task.get_task_by_entity(sequence, task_type)
            if task:
                unreal.log(f"🦊 No '{task_type_name}' task on shot {shot_name}, publishing on sequence {sequence_name}")
        if not task:
            unreal.log_error(f"❌ No '{task_type_name}' task on shot {shot_name} or sequence {sequence_name}")
            return False

        # Get the task status 
        status = gazu.task.get_task_status_by_short_name(task_status_name)
        if not status:
            unreal.log_error(f"❌ Task status '{task_status_name}' not found in Kitsu")
            return False

        unreal.log(f"📤 Uploading {file_path} for {shot_name}")
        unreal.log(f"📝 Full comment: {full_comment}")
        try:
            (_, preview_file) = gazu.task.publish_preview(
                task,
                status,
                comment=full_comment,
                preview_file_path=file_path,
            )
            unreal.log(f"🦊 ✅ Published preview {file_path}")
        except Exception as e:
            unreal.log_warning(f"🦊 ⚠️ Failed to publish {file_path}: {e}")
            return False

        return True

    except Exception as e:
        unreal.log_error(f"🦊 ❌ Upload failed for {shot_name}: {e}")
        return False


def count_video_frames(movie_path: str):
    """
    Number of frames in the first video stream of a movie, read with ffprobe.
    Returns None if ffprobe is not installed, 0 if the file can't be read
    (e.g. truncated because the encode was killed).
    """
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None

    cmd = [
        ffprobe, "-v", "error", "-select_streams", "v:0",
        "-count_packets", "-show_entries", "stream=nb_read_packets",
        "-of", "csv=p=0", movie_path,
    ]
    try:
        out = subprocess.run(
            cmd, capture_output=True, text=True, timeout=300,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return int(out.stdout.strip())
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        unreal.log_warning(f"⚠️ ffprobe could not read {movie_path}: {e}")
        return 0


class _UnrealLogHandler(logging.Handler):
    """Forwards the daily library's logging to the Unreal Output Log."""

    def emit(self, record):
        msg = f"🎞️ {self.format(record)}"
        if record.levelno >= logging.ERROR:
            unreal.log_error(msg)
        elif record.levelno >= logging.WARNING:
            unreal.log_warning(msg)
        else:
            unreal.log(msg)


def _route_daily_logs():
    logger = logging.getLogger("daily")
    if not any(isinstance(h, _UnrealLogHandler) for h in logger.handlers):
        logger.addHandler(_UnrealLogHandler())
        logger.setLevel(logging.INFO)
        logger.propagate = False


def read_exr_frame_rate(exr_path: str):
    """
    Frame rate Unreal rendered at (unreal/frameRate in the EXR header) as a string
    for daily ("24", "25", "29.97"), or None if it's not there.
    """
    try:
        import OpenEXR
        value = OpenEXR.File(exr_path, header_only=True).header().get("unreal/frameRate")
        if value is None:
            unreal.log("🎞️ No unreal/frameRate in the EXR header, using the daily config frame rate")
            return None
        fps = float(value)
    except Exception as e:
        unreal.log_warning(f"⚠️ Could not read the frame rate from {exr_path}: {e}")
        return None
    if fps <= 0:
        return None
    return str(int(fps)) if fps.is_integer() else f"{fps:g}"


def _proxy_frames(first_file: str, rendered: list[int], frame_range):
    """
    Frame numbers to put in the proxy: the whole sequence (frame_range, from
    get_sequence_frame_range) when all its frames are on disk, even if this job rendered
    only part of it. Otherwise only the frames this job rendered. Never stale frames
    outside the sequence, and never a proxy with holes.
    """
    import pyseq
    if not frame_range:
        return rendered
    first, last = frame_range
    if not all(first <= f <= last for f in rendered):
        unreal.log_warning(f"⚠️ Rendered frames {rendered[0]}-{rendered[-1]} are outside the sequence range {first}-{last} "
                           f"(file name not using {{frame_number}}?): the proxy only has the {len(rendered)} rendered frames")
        return rendered

    name = os.path.basename(first_file)
    on_disk = next((s for s in pyseq.get_sequences(os.path.dirname(first_file)) if any(item.name == name for item in s)), [])
    on_disk = {item.frame for item in on_disk}
    missing = [f for f in range(first, last + 1) if f not in on_disk]
    if missing:
        unreal.log_warning(f"⚠️ {len(missing)} frames of the sequence ({first}-{last}) are not on disk: "
                           f"the proxy only has the {len(rendered)} rendered frames")
        return rendered

    count = last - first + 1
    if count > len(rendered):
        unreal.log(f"🎞️ Proxy covers the whole sequence {first}-{last}: {len(rendered)} frames rendered now, {count - len(rendered)} from earlier renders")
    return list(range(first, last + 1))


def generate_proxy(image_file_paths: list[str], frame_rate: str = None, frame_range=None):
    """
    Runs daily (vendored Rapheus/daily, config in daily_config/) on the image sequence
    of the first rendered file, to produce a single proxy .mp4 in a /proxy subfolder next to it.
    The frames encoded are picked by _proxy_frames.

    Returns (mp4_path, daily_info), or (None, {}) if no complete proxy was made:
    the mp4 must have exactly as many frames as were picked.
    """
    from pathlib import Path
    try:
        import pyseq
        from daily.config import build_daily_config
        from daily.daily import run as run_daily
    except ImportError as e:
        unreal.log_error(f"❌ Could not import daily: {e}")
        return None, {}
    _route_daily_logs()

    config_dir = Path(os.path.dirname(os.path.abspath(__file__))) / "daily_config"

    # Only the sequence of the first file: other render layers / passes are not proxied.
    # pyseq ignores folders when given a list, so filter on the folder first.
    first_file = os.path.normpath(image_file_paths[0])
    input_dir = os.path.dirname(first_file)
    same_dir = [f for f in image_file_paths if os.path.normcase(os.path.dirname(os.path.normpath(f))) == os.path.normcase(input_dir)]
    wanted = os.path.normcase(first_file)
    sequence = next(
        (s for s in pyseq.get_sequences(same_dir) if any(os.path.normcase(os.path.normpath(item.path)) == wanted for item in s)),
        None,
    )
    rendered_count = len(sequence) if sequence else 1
    if len(image_file_paths) > rendered_count:
        unreal.log_warning(f"⚠️ {len(image_file_paths) - rendered_count} rendered files belong to other sequences/layers and are not proxied")

    # A still has no frame number: daily encodes it alone, no frame selection
    frames = None
    if sequence and all(isinstance(item.frame, int) for item in sequence):
        frames = _proxy_frames(first_file, sorted(item.frame for item in sequence), frame_range)
    expected_frames = len(frames) if frames else rendered_count

    name = (sequence.head().rstrip("._- ") if sequence and len(sequence) > 1 else "") or os.path.splitext(os.path.basename(first_file))[0]
    mp4_path = os.path.join(input_dir, "proxy", f"{name}.mp4")

    overrides = {}
    # From the job (get_render_frame_rate); the EXR header is only a fallback, see there
    frame_rate = frame_rate or read_exr_frame_rate(first_file)
    if frame_rate:
        overrides["output.framerate"] = frame_rate

    try:
        config = build_daily_config(
            input_path=first_file,
            output=mp4_path,
            config_path=config_dir / "daily.yaml",
            codecs_path=config_dir / "codecs.yaml",
            text_overlays_path=config_dir / "text_overlays.yaml",
            set_overrides=overrides,
        )
    except Exception as e:
        unreal.log_error(f"❌ Invalid daily config in {config_dir}: {e}")
        return None, {}
    config.frame_numbers = frames

    transform = config.ocio.transform
    unreal.log(f"🎞️ Running daily on {first_file} ({rendered_count} frames rendered, {expected_frames} in the proxy, {config.output.framerate} fps)")
    unreal.log(f"🎞️ Proxy: {mp4_path}")

    # Pass a file: daily then encodes only the sequence it belongs to
    try:
        produced = run_daily(config)
    except Exception as e:
        # Unreadable frame (strict_frames), ffmpeg failure...: never upload a partial proxy
        unreal.log_error(f"❌ daily failed, proxy not uploaded: {e!r}")
        try:
            os.remove(mp4_path)
        except OSError:
            pass
        return None, {}

    if [os.path.normcase(str(p)) for p in produced] != [os.path.normcase(os.path.normpath(mp4_path))]:
        unreal.log_error(f"❌ daily produced {[str(p) for p in produced]}, expected exactly {mp4_path}")
        return None, {}

    frame_count = count_video_frames(mp4_path)
    expected_total = expected_frames + (config.slate.duration_frames if config.slate.enable else 0)
    if frame_count is None:
        unreal.log_warning("⚠️ ffprobe not found, can't check the proxy's frame count on disk")
    elif frame_count != expected_total:
        unreal.log_error(f"❌ Proxy has {frame_count} frames, expected {expected_total}. Not uploaded: {mp4_path}")
        return None, {}

    if transform.type == "display":
        color = [transform.src, f"{transform.display} / {transform.view}"]
    else:
        color = [transform.src, transform.dst]
    daily_info = {
        "ocio_transform": color,
        "codec": config.output.codec,
        "resolution": config.output.resolution,
        "framerate": config.output.framerate,
    }
    return mp4_path, daily_info
