"""Defer direct base shots long enough to let cooperative ingress make room."""


def install():
    """Patch robot assault scheduling without changing its targeting semantics.

    The normal assault loop locks direct base shooters before ``_make_room_inside``.
    When another robot is waiting without a base shot, defer that early lock so
    the existing make-room planner can consider the shooter as a movable front
    robot.  If no useful yielding move exists, the later ordinary base-shot pass
    still attacks in the same turn.
    """
    from . import robot_assault

    original = robot_assault._act_toward_target
    if getattr(original, "_make_room_before_attack", False):
        return

    def act_toward_target(turn, memory, nav, ledger, robot, target, occupied, *,
                          attack_only=False, direct_base=False):
        if (attack_only and direct_base and target.kind == "station"
                and not turn.is_day and robot.abnormal_state != "dizzy"):
            waiting = any(
                other.id != robot.id
                and other.id not in ledger.used
                and other.abnormal_state != "dizzy"
                and robot_assault._firing_point(
                    other.pos, other, target, memory, occupied) is None
                for other in turn.summon_robots
            )
            if waiting:
                # Do not spend the robot's action yet. _make_room_inside runs
                # before the later base-shot allocation and may move this
                # shooter deeper to open a lane. If it cannot improve ingress,
                # the normal later pass still gives this robot its base attack.
                return False
        return original(turn, memory, nav, ledger, robot, target, occupied,
                        attack_only=attack_only, direct_base=direct_base)

    act_toward_target._make_room_before_attack = True
    robot_assault._act_toward_target = act_toward_target
