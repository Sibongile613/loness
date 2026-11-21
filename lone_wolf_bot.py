"""
Lone Wolf Labs bot v6-K - KILLER EDITION
for Builders - Mech Royale (mechfight/1).

Based on v6, which scored 2.42.

Killing improvements:
- Strongly prefers weak, finishable enemies.
- Stays on a target longer to secure kills.
- Fires sooner while tracking.
- Leads moving targets slightly better.
- Releases lasers before overcharging.
- Uses lightning and mortar more aggressively.
- Reloads before entering a fight.
"""

from __future__ import annotations

import array
import json
import math
import os
import random
import sys
from typing import Any, Dict, List, Optional, Tuple

import mechfight as mf


def _wrap_angle(angle: float) -> float:
    while angle > math.pi:
        angle -= 2.0 * math.pi
    while angle < -math.pi:
        angle += 2.0 * math.pi
    return angle


def _dist2d(a: List[float], b: List[float]) -> float:
    return math.hypot(a[0] - b[0], a[1] - b[1])


class Terrain:
    def __init__(self, map_dir: str) -> None:
        self.ok = False
        try:
            base = os.path.join(map_dir, "v2")
            with open(os.path.join(base, "world.json"), "r", encoding="utf-8") as f:
                meta = json.load(f)["terrain"]
            self.min_x = float(meta["minX"])
            self.min_y = float(meta["minY"])
            self.step = float(meta["step"])
            self.nx = int(meta["nx"])
            self.ny = int(meta["ny"])
            data = array.array("h")
            with open(os.path.join(base, str(meta.get("file", "terrain.i16"))), "rb") as f:
                data.fromfile(f, self.nx * self.ny)
            if sys.byteorder == "big":
                data.byteswap()
            self.data = data
            self.ok = True
        except Exception:
            self.ok = False

    def ground(self, x: float, y: float) -> Optional[float]:
        if not self.ok:
            return None
        i = max(0, min(self.nx - 1, int(round((x - self.min_x) / self.step))))
        j = max(0, min(self.ny - 1, int(round((y - self.min_y) / self.step))))
        return self.data[j * self.nx + i] / 100.0


class Bot:
    name = "lone-wolf-v6-killer"

    def __init__(self, hello: mf.Hello) -> None:
        cfg = hello.get("config", {})
        loot_cfg = cfg.get("loot", {})
        self.pickup_radius = float(loot_cfg.get("pickup_radius", 4.0))
        self.upgrades_cfg = cfg.get("upgrades", [])
        self.upgrade_priority = {}

        for upgrade in self.upgrades_cfg:
            uid = str(upgrade.get("id", ""))
            effects = upgrade.get("effects", [])
            score = 0

            for effect in effects:
                path = str(effect.get("path", ""))

                if "damage" in path or "rate" in path or "cooldown" in path:
                    score += 35
                if "speed" in path or "boost" in path or "energy" in path:
                    score += 25
                if "armor" in uid or "rear" in path or "max_hp" in path or "regen" in path:
                    score += 20
                if "sensor" in uid or "vision" in path or "sound" in path:
                    score += 15

            self.upgrade_priority[uid] = score

        self.rng = random.Random(int(hello.get("seed", 1234567)))
        self.last_damage_t = -999.0
        self.last_damage_bearing = None
        self.laser_charging = [False, False]
        self.terrain = Terrain(str(hello.get("map", {}).get("dir", "/arena")))
        self.skip_pickups = {}
        self.loot_mem = None
        self.last_pos = None
        self.stuck_ticks = 0
        self._strafe_dir = self.rng.choice([-1.0, 1.0])
        self._strafe_until = 0.0
        self._hunt_target_id = None
        self._hunt_until = 0.0

    def _pick_upgrade(self, offer: Dict[str, Any]) -> int:
        choices = offer.get("choices", [])

        if not choices:
            return 0

        best_index = 0
        best_score = -10**9

        for index, choice in enumerate(choices):
            uid = str(choice.get("id", ""))
            description = str(choice.get("desc", "")).lower()
            score = self.upgrade_priority.get(uid, 0)

            if "damage" in description or "rate" in description or "cooldown" in description:
                score += 30
            if "speed" in description or "energy" in description or "boost" in description:
                score += 20
            if "armor" in description or "hp" in description or "regen" in description:
                score += 15
            if "vision" in description or "sensor" in description or "sound" in description:
                score += 10

            if score > best_score:
                best_score = score
                best_index = index

        return best_index

    def _zone_target(self, obs: mf.Observation, x: float, y: float) -> Tuple[float, float]:
        zone = obs["zone"]
        t = float(obs["t"])
        shrink_start = float(zone.get("shrink_start", t))
        shrink_end = float(zone.get("shrink_end", t + 1.0))
        current_center = zone.get("center", [0.0, 0.0])
        current_radius = float(zone.get("radius", 500.0))
        next_center = zone.get("next_center", current_center)
        next_radius = float(zone.get("next_radius", current_radius))

        if t >= shrink_start:
            if shrink_end > shrink_start:
                progress = max(0.0, min(1.0, (t - shrink_start) / (shrink_end - shrink_start)))
            else:
                progress = 1.0

            center_x = current_center[0] + (next_center[0] - current_center[0]) * progress
            center_y = current_center[1] + (next_center[1] - current_center[1]) * progress
            radius = current_radius + (next_radius - current_radius) * progress
        else:
            center_x, center_y, radius = current_center[0], current_center[1], current_radius

        safe_radius = max(5.0, radius * 0.82)
        delta_x = next_center[0] - center_x
        delta_y = next_center[1] - center_y
        delta_length = math.hypot(delta_x, delta_y)

        if delta_length > 1e-6:
            target_x = center_x + (delta_x / delta_length) * min(delta_length * 0.75, safe_radius * 0.55)
            target_y = center_y + (delta_y / delta_length) * min(delta_length * 0.75, safe_radius * 0.55)
        else:
            target_x, target_y = center_x, center_y

        current_distance = math.hypot(x - center_x, y - center_y)
        next_distance = math.hypot(x - next_center[0], y - next_center[1])

        if current_distance < safe_radius and next_distance > next_radius * 0.90:
            return float(next_center[0]), float(next_center[1])

        return float(target_x), float(target_y)

    def _fire_decision(
        self,
        slot: int,
        weapon: Optional[mf.WeaponObs],
        distance: float,
        has_target: bool,
        energy: float,
        target_has_mortar: bool,
    ) -> bool:
        if weapon is None or not has_target:
            self.laser_charging[slot] = False
            return False

        kind = weapon.get("kind")
        stats = weapon.get("stats", {})
        max_range = float(stats.get("range", 200.0))

        if kind == "gun":
            ammo = float(weapon.get("ammo", 0.0))
            reload_left = float(weapon.get("reload", 0.0))

            if reload_left > 0.0 or ammo <= 0.0:
                return False

            return distance <= max_range * 0.98

        if kind == "lightning":
            if energy < 10.0 or distance > min(50.0, max_range):
                return False

            return float(weapon.get("cooldown", 0.0)) <= 0.05

        if kind == "mortar":
            if distance < 20.0 or distance > max_range:
                return False

            return float(weapon.get("cooldown", 0.0)) <= 0.05

        if kind == "laser":
            charge_time = float(weapon.get("charge", 0.0))
            full_charge = float(stats.get("charge", 0.75))

            if distance > max_range:
                self.laser_charging[slot] = False
                return False

            target_charge = full_charge * (0.78 if target_has_mortar else 0.90)

            if not self.laser_charging[slot]:
                if float(weapon.get("cooldown", 0.0)) <= 0.05 and energy > 12.0:
                    self.laser_charging[slot] = True
                    return True

                return False

            if charge_time >= target_charge:
                self.laser_charging[slot] = False
                return False

            return True

        return False

    def act(self, obs: mf.Observation) -> mf.Action:
        me = obs["self"]
        x, y, z = float(me["pos"][0]), float(me["pos"][1]), float(me["pos"][2])
        hp = float(me.get("hp", 1400.0))
        max_hp = max(1.0, float(me.get("max_hp", 1400.0)))
        hp_ratio = hp / max_hp
        energy = float(me.get("energy", 0.0))
        in_zone = bool(me.get("in_zone", True))
        t = float(obs["t"])
        my_stats = me.get("stats", {})
        muzzle_z = z + float(my_stats.get("muzzle_height", 7.0))

        for event in obs.get("events", []):
            if event.get("type") == "damaged":
                self.last_damage_t = t

                if event.get("bearing") is not None:
                    self.last_damage_bearing = float(event["bearing"])

            elif event.get("type") == "take_failed":
                self.skip_pickups[int(event.get("id", -1))] = t + 25.0

        if self.last_pos is not None:
            moved = math.hypot(x - self.last_pos[0], y - self.last_pos[1])

            if moved < 0.12:
                self.stuck_ticks += 1
            else:
                self.stuck_ticks = 0
        else:
            self.stuck_ticks = 0

        self.last_pos = (x, y)

        action: mf.Action = {"tick": obs["tick"]}

        if obs.get("offer"):
            action["pick"] = self._pick_upgrade(obs["offer"])  # type: ignore[arg-type]

        weapons = me.get("weapons", [None, None])
        gun_slot = weapons[0] if len(weapons) > 0 and weapons[0] and weapons[0].get("kind") == "gun" else None

        bullet_speed = 252.0

        if gun_slot is not None:
            bullet_speed = max(1.0, float(gun_slot.get("stats", {}).get("speed", 252.0)))
            ammo = float(gun_slot.get("ammo", 0.0))
            magazine = float(gun_slot.get("stats", {}).get("mag", 12.0))
            reload_left = float(gun_slot.get("reload", 0.0))

            if reload_left <= 0.0 and ammo <= max(2.0, magazine * 0.30):
                action["reload"] = True

        pickups = obs.get("pickups", [])

        if len(weapons) > 1 and weapons[1] is None:
            for pickup in pickups:
                if pickup.get("kind") in ("laser", "lightning", "mortar"):
                    if _dist2d(pickup["pos"], [x, y]) <= self.pickup_radius:
                        action["take"] = pickup["id"]
                        break

        mechs = obs.get("mechs", [])
        enemies = [mech for mech in mechs if mech.get("kind") == "mech"]

        zone = obs["zone"]
        zone_center = zone.get("center", [0.0, 0.0])
        zone_radius = float(zone.get("radius", 500.0))

        nearest_distance = min(
            (math.hypot(enemy["pos"][0] - x, enemy["pos"][1] - y) for enemy in enemies),
            default=999.0,
        )

        recently_hit = (t - self.last_damage_t) < 4.0
        critical_hp = hp_ratio < 0.18

        # Choose the best kill target:
        # damaged enemies first, then close enemies, then cube carriers.
        target = None

        if enemies:
            def kill_score(enemy: Dict[str, Any]) -> float:
                distance = math.dist(enemy["pos"], [x, y, z])
                enemy_hp = float(enemy.get("hp", 1400.0))
                enemy_max_hp = max(1.0, float(enemy.get("max_hp", 1400.0)))
                enemy_hp_ratio = enemy_hp / enemy_max_hp
                cubes = int(enemy.get("cubes", 0))

                return distance + enemy_hp_ratio * 35.0 - cubes * 8.0

            target = min(enemies, key=kill_score)

            if self._hunt_target_id is None or t > self._hunt_until:
                self._hunt_target_id = target.get("id")
                self._hunt_until = t + 3.0

            for enemy in enemies:
                if enemy.get("id") == self._hunt_target_id:
                    target = enemy
                    break

        loot_target = None
        loot_distance = 0.0

        if len(weapons) > 1 and weapons[1] is None and in_zone:
            held_weapons = {weapon.get("kind") for weapon in weapons if weapon}
            best_score = -1e9

            for pickup in pickups:
                pickup_kind = pickup.get("kind")

                if pickup_kind not in ("laser", "lightning", "mortar") or pickup_kind in held_weapons:
                    continue

                if self.skip_pickups.get(int(pickup["id"]), -1.0) > t:
                    continue

                ground = self.terrain.ground(float(pickup["pos"][0]), float(pickup["pos"][1]))

                if ground is not None and float(pickup["pos"][2]) - ground > 2.0:
                    continue

                distance = _dist2d(pickup["pos"], [x, y])

                if distance > 250.0:
                    continue

                value = {
                    "laser": 55.0,
                    "lightning": 35.0,
                    "mortar": 15.0,
                }[pickup_kind]

                score = value - 0.22 * distance

                if nearest_distance < 45.0:
                    score -= 25.0

                if score > best_score:
                    best_score = score
                    loot_target = pickup
                    loot_distance = distance

        if loot_target is not None:
            self.loot_mem = {
                "id": int(loot_target["id"]),
                "kind": loot_target["kind"],
                "pos": [
                    float(loot_target["pos"][0]),
                    float(loot_target["pos"][1]),
                    float(loot_target["pos"][2]),
                ],
                "seen": t,
            }
        elif self.loot_mem is not None:
            memory = self.loot_mem
            memory_distance = _dist2d(memory["pos"], [x, y])

            gone = (
                len(weapons) <= 1
                or weapons[1] is not None
                or not in_zone
                or t - memory["seen"] > 7.0
                or memory_distance < 35.0
                or self.skip_pickups.get(memory["id"], -1.0) > t
            )

            if gone:
                self.loot_mem = None
            else:
                loot_target = memory
                loot_distance = memory_distance

        if loot_target is not None and self.stuck_ticks >= 10:
            self.skip_pickups[int(loot_target["id"])] = t + 12.0
            self.loot_mem = None
            loot_target = None

        zone_x, zone_y = self._zone_target(obs, x, y)

        move_x = zone_x - x
        move_y = zone_y - y
        move_mode = "zone_advance"

        my_zone_distance = math.hypot(x - zone_center[0], y - zone_center[1])

        if not in_zone or my_zone_distance >= zone_radius * 0.92:
            move_x = zone_x - x
            move_y = zone_y - y
            move_mode = "zone_escape"

        elif critical_hp and enemies:
            closest_enemy = min(
                enemies,
                key=lambda enemy: math.hypot(enemy["pos"][0] - x, enemy["pos"][1] - y),
            )

            away_x = x - closest_enemy["pos"][0]
            away_y = y - closest_enemy["pos"][1]
            away_length = max(1e-6, math.hypot(away_x, away_y))

            move_x = (away_x / away_length) * 80.0 + (zone_x - x) * 0.45
            move_y = (away_y / away_length) * 80.0 + (zone_y - y) * 0.45
            move_mode = "critical_retreat"

        elif target is not None:
            target_x, target_y, target_z = target["pos"]
            target_distance = math.hypot(target_x - x, target_y - y)
            target_yaw = math.atan2(target_y - y, target_x - x)

            if self.rng.random() < 0.04 or t > self._strafe_until:
                self._strafe_dir = self.rng.choice([-1.0, 1.0])
                self._strafe_until = t + self.rng.uniform(1.0, 2.5)

            strafe_yaw = target_yaw + self._strafe_dir * (math.pi * 0.5)

            if hp_ratio > 0.55:
                desired_range = 45.0
            elif hp_ratio > 0.30:
                desired_range = 65.0
            else:
                desired_range = 90.0

            radial_pull = 0.0

            if target_distance < desired_range * 0.75:
                radial_pull = -0.65
            elif target_distance > desired_range * 1.35 and in_zone:
                radial_pull = 0.35

            move_x = (
                (zone_x - x) * (1.2 if not in_zone else 0.45)
                + math.cos(target_yaw) * radial_pull * 40.0
                + math.cos(strafe_yaw) * 28.0
            )
            move_y = (
                (zone_y - y) * (1.2 if not in_zone else 0.45)
                + math.sin(target_yaw) * radial_pull * 40.0
                + math.sin(strafe_yaw) * 28.0
            )

            move_mode = "hunt_and_kill"

        elif loot_target is not None:
            move_x = float(loot_target["pos"][0]) - x
            move_y = float(loot_target["pos"][1]) - y
            move_mode = "loot_rush"

        move_length = math.hypot(move_x, move_y)

        if move_length > 1e-6:
            speed_scale = 0.38 if (
                critical_hp
                and not recently_hit
                and in_zone
                and len(enemies) == 0
            ) else 1.0

            action["move"] = [
                (move_x / move_length) * speed_scale,
                (move_y / move_length) * speed_scale,
            ]
        else:
            action["move"] = [0.0, 0.0]

        should_boost = (
            (not in_zone and energy > 20.0)
            or (recently_hit and critical_hp and energy > 35.0)
            or (loot_target is not None and loot_distance > 30.0 and energy > 40.0)
        )

        action["boost"] = bool(should_boost)
        action["jump"] = bool(
            me.get("grounded", False)
            and self.rng.random() < 0.02
            and len(enemies) > 0
        )

        if target is not None:
            target_position = target["pos"]
            target_velocity = target.get("vel", [0.0, 0.0, 0.0])
            horizontal_distance = max(1.0, math.hypot(target_position[0] - x, target_position[1] - y))

            lead_time = min(1.1, horizontal_distance / bullet_speed)
            predicted_x = target_position[0] + target_velocity[0] * lead_time * 1.05
            predicted_y = target_position[1] + target_velocity[1] * lead_time * 1.05

            target_cubes = int(target.get("cubes", 0))
            predicted_z = target_position[2] + 4.5 * (1.0 + 0.05 * min(target_cubes, 10))

            yaw = math.atan2(predicted_y - y, predicted_x - x)
            pitch = math.atan2(predicted_z - muzzle_z, horizontal_distance)
            pitch = max(-0.8, min(0.8, pitch))

            action["aim"] = [yaw, pitch]

            target_weapons = target.get("weapons", [])
            target_has_mortar = "mortar" in target_weapons

            fire_primary = self._fire_decision(
                0,
                weapons[0] if len(weapons) > 0 else None,
                horizontal_distance,
                True,
                energy,
                target_has_mortar,
            )

            fire_secondary = self._fire_decision(
                1,
                weapons[1] if len(weapons) > 1 else None,
                horizontal_distance,
                True,
                energy,
                target_has_mortar,
            )

            fire = [fire_primary, fire_secondary]

            my_aim = me.get("aim", [yaw, pitch])
            aim_error = abs(_wrap_angle(float(my_aim[0]) - yaw))

            for index, weapon in enumerate(weapons[:2]):
                if weapon is not None and weapon.get("kind") == "laser" and self.laser_charging[index]:
                    continue

                if aim_error > 0.07:
                    fire[index] = False

            action["fire"] = fire

            mf.draw.line(
                [x, y, muzzle_z],
                [predicted_x, predicted_y, predicted_z],
                "#ff5040",
            )

        else:
            self.laser_charging = [False, False]

            if recently_hit and self.last_damage_bearing is not None:
                action["aim"] = [_wrap_angle(self.last_damage_bearing), 0.0]
            elif loot_target is not None:
                action["aim"] = [
                    math.atan2(float(loot_target["pos"][1]) - y, float(loot_target["pos"][0]) - x),
                    0.0,
                ]
            else:
                action["aim"] = [math.atan2(zone_y - y, zone_x - x), 0.0]

            action["fire"] = [False, False]

        mf.draw.point([zone_x, zone_y, z], "#40c0ff", 6)
        mf.draw.label(
            f"KILLER v6-K | hp {hp:.0f}/{max_hp:.0f} | "
            f"mode {move_mode} | enemies {len(enemies)} | "
            f"nearest {nearest_distance:.0f}"
        )

        return action


mf.run(Bot)