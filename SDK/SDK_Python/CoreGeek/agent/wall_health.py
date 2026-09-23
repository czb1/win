"""Wall HP from the task book 4.5.1 attribute table, confirmed by its image."""

# Level 1/2/3: 1000/1500/2000. Keep every night WallFixer entry point in sync.
WALL_MAX_HEALTH = (1000, 1500, 2000)


def needs_night_repair(wall):
    # Strictly below 10%; exactly 100/150/200 HP must not consume a pack.
    return (wall.kind == "wall" and 0 < wall.health
            and wall.health * 10 < WALL_MAX_HEALTH[wall.level - 1])
