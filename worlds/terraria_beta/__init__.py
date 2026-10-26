# Look at `Rules.dsv` first to get an idea for how this works

import dataclasses
import logging
import math
import random
from typing import Union, Tuple, List, Dict, Set, override

from Options import NumericOption
from rule_builder.cached_world import CachedRuleBuilderWorld
from rule_builder.field_resolvers import FromWorldAttr
from worlds.AutoWorld import WebWorld, World
from BaseClasses import Region, ItemClassification, Tutorial, CollectionState, Spoiler, MultiWorld, ThreadBarrierProxy
from rule_builder.options import OptionFilter
from rule_builder.rules import Has, HasAny, HasAll, True_, False_, And, Or, Rule, TWorld, HasFromList, Filtered, \
    HasAnyCount
from .Flags import (
    invalid_flag,
    is_location_is_item, special_events,
)
from .Checks import (
    TerrariaItem,
    TerrariaLocation,
    Condition,
    goals,
    rules,
    rule_indices,
    labels,
    rewards,
    item_name_to_id,
    location_name_to_id,
    loc_to_item,
    COND_ITEM,
    COND_LOC,
    COND_FN,
    COND_GROUP,
    progressive_to_count,
    npcs,
    pickaxes,
    hammers,
    mech_bosses,
    armor_minions,
    accessory_minions,
    health_upgrades,
    quarter_fruits,
)
from .Options import TerrariaOptions, Goal, ter_option_groups, RandomizeNPCs, Mods, RareAchievements, \
    TimeAchievements, HealthLogic, Getfixedboi, ShimmerSkips, FishingLogic, Researchsanity, \
    ResearchLockedBehindItems, RandomizeDepth


class TerrariaWeb(WebWorld):
    tutorials = [
        Tutorial(
            "Multiworld Setup Guide",
            "A guide to setting up the Terraria randomizer connected to an Archipelago Multiworld.",
            "English",
            "setup_en.md",
            "setup/en",
            ["Seldom"],
        )
    ]
    option_groups = ter_option_groups


class TerrariaSpoiler(Spoiler):
    def hidden_locations(self) -> Set[str]:
        hidden = set()

        for loc in self.multiworld.get_filled_locations():
            if self.multiworld.game[loc.player] != "Terraria Beta":
                continue

            options = self.multiworld.worlds[loc.player].options
            if not options.compressed_playthrough.value or not loc.is_event:
                continue

            rule = rules[rule_indices[loc.name]]
            if options.health_logic.value and ("Health" in rule.flags or "Quarter Fruit" in rule.flags):
                continue

            hidden.add(loc.name + f" ({self.multiworld.get_player_name(loc.player)})" if self.multiworld.players > 1 else loc.name)

        return hidden

    def create_playthrough(self, create_paths: bool = True) -> None:
        super().create_playthrough(create_paths)

        hidden_locs = self.hidden_locations()

        visible_spheres = []

        for sphere in sorted(int(num) for num in self.playthrough if num != "0"):
            sphere = self.playthrough[str(sphere)]
            visible = {
                loc: item
                for loc, item in sphere.items()
                if loc not in hidden_locs
            }

            if visible:
                visible_spheres.append(visible)

        self.playthrough = {
            "0": self.playthrough.get("0", []),
            **{
                str(num): sphere
                for num, sphere in enumerate(visible_spheres, start=1)
            },
        }

        if create_paths:
            self.paths = {
                location: path
                for location, path in self.paths.items()
                if location not in hidden_locs
            }


class TerrariaWorld(CachedRuleBuilderWorld):
    """
    Terraria is a 2D multiplayer sandbox game featuring mining, building, exploration, and combat.
    Features 18 bosses and 4 classes.
    """
    game = "Terraria Beta"
    web = TerrariaWeb()
    options_dataclass = TerrariaOptions
    options: TerrariaOptions

    item_name_to_id = item_name_to_id
    location_name_to_id = location_name_to_id

    calamity = False
    fargo = False
    getfixedboi = False

    is_ut = False
    ut_can_gen_without_yaml = True

    required_client_version = (0, 6, 200)
    
    OUT_OF_LOGIC = "UT Health Logic"
    if OUT_OF_LOGIC not in item_name_to_id:
        item_name_to_id[OUT_OF_LOGIC] = max(item_name_to_id.values()) + 1

    def __init__(self, multiworld: MultiWorld, player: int):
        super().__init__(multiworld, player)
        self.progression = set()
        self.useful_if_not_progression = set()
        self.npcs_to_randomize = set()

        self.ter_items: List[str]
        self.ter_locations: List[str]
        self.ter_rules = dict()

        self.ter_goals: Dict[str, str]
        self.goal_items: Set[str]
        self.goal_locations: Set[str]

        self.disabled_rules: Set[str]

    # Yamlless UT Support
    @staticmethod
    def interpret_slot_data(slot_data: Dict[str, object]) -> Dict[str, object]:
        return {
            "goal": slot_data["goal_option"],
            "shuffle_to": slot_data["shuffle_to_option"],
            "death_link": slot_data["deathlink"],
            "mods": slot_data["mods"],
            "getfixedboi": slot_data["getfixedboi"],
            "randomize_npcs": slot_data["npc_rando"],
            "randomize_depth": slot_data["depth_rando"],
            "early_achievements": slot_data["early_achievements"],
            "normal_achievements": slot_data["normal_achievements"],
            "rare_achievements": slot_data["rare_achievements"],
            "time_achievements": slot_data["time_achievements"],
            "crafting_achievements": slot_data["crafting_achievements"],
            "grindy_achievements": slot_data["grindy_achievements"],
            "fishing_achievements": slot_data["fishing_achievements"],
            "fishing_logic": slot_data["fishing_logic"],
            "researchsanity": slot_data["researchsanity"],
            "researchsanity_seed": slot_data["researchsanity_seed"],
            "research_locked_behind_items": slot_data["research_locked_behind_items"],
            "fill_extra_checks_with": slot_data["fill_extra_checks_with"],
            "shimmer_skips": slot_data["shimmer_skips"],
            "health_logic": slot_data["health_logic"],
            "health_logic_handicap": slot_data["health_logic_handicap"],
        }
    
    def generate_early(self) -> None:
        self.is_ut = getattr(self.multiworld, "generation_is_fake", False)
        
        # UT passes the connected slot's options through re_gen_passthrough when
        # generating the world without a YAML. Restore those options before generation.
        passthrough = getattr(self.multiworld, "re_gen_passthrough", {})
        
        if self.game in passthrough:
            slot_data = passthrough[self.game]

            for option_name, value in slot_data.items():
                if option_name not in self.options_dataclass.type_hints:
                    continue
                
                option_type = self.options_dataclass.type_hints[option_name]
                setattr(self.options, option_name, option_type.from_any(value))
        
        if not isinstance(self.multiworld.spoiler, TerrariaSpoiler):
            self.multiworld.spoiler = TerrariaSpoiler(self.multiworld)
        
        self.glitches_item_name = self.OUT_OF_LOGIC

        goal, goal_locations = goals[self.options.goal.value]
        slot_name = self.multiworld.player_name[self.player]
        match self.options.shuffle_to.value:
            case 0:
                pass
            case -1:
                goal = 0
            case _:
                if self.options.shuffle_to.value < self.options.goal.value:
                    logging.warning(f"SLOT {slot_name}: \"Shuffle To\" value ({Goal.name_lookup[self.options.shuffle_to.value]}) was set earlier than the goal ({Goal.name_lookup[self.options.goal.value]}); ignoring")
                else:
                    goal, _ = goals[self.options.shuffle_to.value]
        ter_goals = {}
        goal_items = set()

        for location in goal_locations:
            if location == "Wall of Flesh" and not self.options.randomize_npcs.value:
                logging.warning(
                    f"SLOT {slot_name}: goal 'Wall of Flesh' was enabled was selected with NPC Randomization disabled. The resulting game will be goalable from Sphere 1."
                )
            flags = rules[rule_indices[location]].flags
            if "Calamity" not in self.options.mods.value and "Calamity" in flags:
                logging.warning(
                    f"SLOT {slot_name}: goal `{Goal.name_lookup[self.options.goal.value]}`, which requires Calamity, was selected with Calamity disabled; enabling Calamity"
                )
                self.options.mods.value.add("Calamity")
            if "Fargo" not in self.options.mods.value and "Fargo" in flags:
                logging.warning(
                    f"SLOT {slot_name}: goal `{Goal.name_lookup[self.options.goal.value]}`, which requires Fargo, was selected with Fargo disabled; enabling Fargo"
                )
                self.options.mods.value.add("Fargo")

            if "Npc" in flags:
                event = location
            else:
                if ("Research" in flags or "ResearchItem" in flags):
                    event = location
                else:
                    event = flags.get("Item") or f"Post-{location}"
            ter_goals[event] = location
            goal_items.add(event)
            
        if self.options.fishing_achievements.value and not self.options.fishing_logic.value:
            logging.warning(
                f"SLOT {slot_name}: Fishing Advancements are enabled but not Fishing Logic; enabling Fishing Logic"
            )
            self.options.fishing_logic.value = 1

        if self.options.getfixedboi:
            incompatible_options = []
            if "Princess" in goal_items:
                incompatible_options.append("Princess goal")
            if self.options.randomize_npcs:
                incompatible_options.append("NPC Randomization")
            if "Fargo" in self.options.mods.value:
                incompatible_options.append("Fargo")
            if self.options.randomize_depth:
                incompatible_options.append("Depth Randomization")
            if self.options.researchsanity.value > 0:
                incompatible_options.append("Researchsanity")

            if incompatible_options:
                logging.warning(f"SLOT {slot_name}: getfixedboi mode is enabled with the following incompatible options:\n"
                                f" - {'\n - '.join(incompatible_options)}\n"
                                f"Disabling getfixedboi.")
                self.options.getfixedboi.value = 0

        valid_rules = rules[:goal] if goal != 0 else rules

        def mark_progression(operator, conditions):
            for condition in conditions:
                if condition.type == COND_ITEM:
                    prog = condition.condition in self.progression
                    self.progression.add(loc_to_item[condition.condition])
                    rule = rules[rule_indices[condition.condition]]
                    if (
                            not prog
                            and not any(is_location_is_item(rule.flags, self.options))
                    ):
                        mark_progression(rule.operator, rule.conditions)
                elif condition.type == COND_LOC:
                    rule = rules[rule_indices[condition.condition]]
                    mark_progression(rule.operator, rule.conditions)
                elif condition.type == COND_GROUP:
                    operator, conds = condition.condition
                    rule = self.create_rule_ini(operator, conds)
                    if isinstance(rule, True_) or isinstance(rule, False_):
                        continue
                    mark_progression(operator, conds)

        # percentageOfSphereOne is an unchanging variable. Probably pretty easy to make this an option, but I figure I'll keep it this way for now. -NonIdiot
        # to note, the "sphereOne" is not actually looking at requirements, but rather if it is tagged with the "SphereOneResearch" flag, which is only placed on locations that are GUARANTEED to be Sphere One (ignoring "Can Research") no matter the options. If added as an option, be sure to mention this in the YAML. -NonIdiot
        percentageOfSphereOne = 15

        # this for loop basically just fills the "potential_researchsanity_rules" list with non-"Item" Rules that are researchsanity ones.
        potential_researchsanity_rules = []
        removable_researchsanity_items = []
        for rule in valid_rules:
            if rule.name in self.disabled_rules:
                continue

            if invalid_flag(rule.flags, self.options) and rule.name not in goal_locations:
                # very buggy. dont use this log -NonIdiot
                #logging.info("Terraria (Beta) says (Code NonIdiot004): "+rule.name+" is invalid.")
                continue
            is_loc, is_item = is_location_is_item(rule.flags, self.options)

            if rule.flags.__contains__("Research") or rule.flags.__contains__("ResearchItem"):
                if is_loc:
                    potential_researchsanity_rules.append(rule)
                elif is_item:
                    removable_researchsanity_items.append(rule)
                    
        # can i use "self.options.researchsanity" instead of "self.options.researchsanity.value > 0"? the world may never know... -NonIdiot
        if self.options.researchsanity.value > 0:
            # if seed is 0, make it not 0. hopefully this never occurs during UT fake gen, but if it does and it causes errors, i can just self.is_ut.
            if self.options.researchsanity_seed.value == 0:
                self.options.researchsanity_seed.value = self.random.randrange(1,999999)

            # then it gets shuffled via the function
            potential_researchsanity_rules = self.shuffle_with_seed(potential_researchsanity_rules,self.options.researchsanity_seed.value)

            # and it is split between two Lists
            potential_researchsanity_sphereone_rules = []
            potential_researchsanity_nonsphereone_rules = []
            for rule in potential_researchsanity_rules:
                if "SphereOneResearch" in rule.flags:
                    potential_researchsanity_sphereone_rules.append(rule)
                else:
                    potential_researchsanity_nonsphereone_rules.append(rule)
            
            # sphere one is removed first (to note, ceil is used on sphereOne and floor is used on nonSphereOne)
            sphereOneWanted = math.ceil(self.options.researchsanity.value*(percentageOfSphereOne/100))
            sphereOnePossible = len(potential_researchsanity_sphereone_rules)
            sphereOneRemoved = 0
            sphereOneToPutInLog = []
            if (sphereOneWanted < sphereOnePossible):
                for num in range(0,sphereOneWanted):
                    sphereOneToPutInLog.append(str(potential_researchsanity_sphereone_rules[num].name).removeprefix("Researchsanity - "))
                for num in range(sphereOneWanted,sphereOnePossible):
                    sphereOneRemoved += 1
                    self.disabled_rules.update({potential_researchsanity_sphereone_rules[num].name})
            else:
                for num in range(0,sphereOnePossible):
                    sphereOneToPutInLog.append(str(potential_researchsanity_sphereone_rules[num].name).removeprefix("Researchsanity - "))

            # then non-sphere one
            nonSphereOneWanted = math.floor(self.options.researchsanity.value*((100-percentageOfSphereOne)/100))
            nonSphereOnePossible = len(potential_researchsanity_nonsphereone_rules)
            nonSphereOneRemoved = 0
            nonSphereOneToPutInLog = []
            if (nonSphereOneWanted < nonSphereOnePossible):
                for num in range(0,nonSphereOneWanted):
                    nonSphereOneToPutInLog.append(str(potential_researchsanity_nonsphereone_rules[num].name).removeprefix("Researchsanity - "))
                for num in range(nonSphereOneWanted,nonSphereOnePossible):
                    nonSphereOneRemoved += 1
                    self.disabled_rules.update({potential_researchsanity_nonsphereone_rules[num].name})
            else:
                for num in range(0,nonSphereOnePossible):
                    nonSphereOneToPutInLog.append(str(potential_researchsanity_nonsphereone_rules[num].name).removeprefix("Researchsanity - "))

            # now, we classify all the AP items
            # never mind, that's done right before pointless_events now
            rules_set_progression = 0
            #totalChecksThere = min(sphereOneWanted,sphereOnePossible)+min(nonSphereOneWanted,nonSphereOnePossible)


            # this is used to debug the Researchsanity functionality. uncomment if you are working on something researchsanity-related. -NonIdiot
            logging.info("Terraria (Beta) says (Code NonIdiot001):"\
                         +"\nFor Researchsanity"\
                         +"\nPotential Locations: "+str(len(potential_researchsanity_rules))\
                         +"\nRemovable Items: "+str(len(removable_researchsanity_items))\
                         +"\nAmount of Sphere One that YAML wants: "+str(sphereOneWanted)\
                         +"\nAmount of possible Sphere One Locations: "+str(sphereOnePossible)\
                         +"\nAmount of Sphere One Locations Disabled: "+str(sphereOneRemoved)\
                         +"\nAmount of Non-Sphere One that YAML wants: "+str(nonSphereOneWanted)\
                         +"\nAmount of possible Non-Sphere One Locations: "+str(nonSphereOnePossible)\
                         +"\nAmount of Non-Sphere One Locations Disabled: "+str(nonSphereOneRemoved)\
                         +"\nSeed Used: "+str(self.options.researchsanity_seed.value)\
                         +"\nrules_set_progression: "+str(rules_set_progression)\
                         +"\nAll Sphere One Locations Allowed (Length "+str(len(sphereOneToPutInLog))+"): "+str(", ".join(sphereOneToPutInLog))\
                         +"\nAll Non-Sphere One Locations Allowed (Length "+str(len(nonSphereOneToPutInLog))+"): "+str(", ".join(nonSphereOneToPutInLog))\
                         )

            

        location_count = 0
        locations = []
        item_count = 0
        items = []

        events = []
        bad_events = set()
        events_to_evaluate = {}

        for rule in valid_rules:            
            if rule.name in self.disabled_rules:
                continue

            if invalid_flag(rule.flags, self.options) and rule.name not in goal_locations:
                continue

            if rule.name in goal_locations:
                self.progression.add(loc_to_item[rule.name])
                mark_progression(rule.operator, rule.conditions)

            for flag in rule.flags:
                if flag in special_events:
                    if flag not in events_to_evaluate:
                        events_to_evaluate[flag] = set()
                    events_to_evaluate[flag].add(rule.name)

            built_rule = self.create_rule_ini(rule.operator, rule.conditions)
            if isinstance(built_rule, False_):
                bad_events.add(rule.name)
            self.ter_rules[rule.name] = built_rule

            is_loc, is_item = is_location_is_item(rule.flags, self.options)

            # useful for logging what is and isn't an item/location. fills up the generation log a TON though.
            #logging.info("Terraria (Beta) says (Code NonIdiot002) "+rule.name+": Is a location? "+str(is_loc)+" Is an item?"+str(is_item))

            if is_loc:
                location_count += 1
                locations.append(rule.name)
                if "Npc" in rule.flags:
                    self.npcs_to_randomize.add(rule.name)
            if is_item and not ("Achievement" in rule.flags and rule.name not in goal_locations):
                # Item
                for i in range(progressive_to_count.get(rule.name) or 1):
                    item_count += 1
                    if rule.name not in goal_locations:
                        items.append(rule.name)
            elif not is_loc and not is_item:
                # Event
                locations.append(rule.name)
                events.append(rule.name)
                items.append(rule.name)



        # this is here to make sure all items fit. totalChecksThere is set to location_count minus the amount of non-"Can Research" items.
        totalChecksThere = location_count-(item_count-20)
        rules_set_progression = 0
        if (totalChecksThere<18 or self.options.research_locked_behind_items.value < 3):
            # if there are less than enough for the FULL list of categories, it is truncated to 3 of the somewhat-biggest, and a combination of the rest, totalling 4.
            do_progression_instead = ["Can Research Stackable Items","Can Research 1-To-Research Accessories","Can Research 1-To-Research Weapons","Can Research 1-To-Research Other Items"]
            if (totalChecksThere<4 or self.options.research_locked_behind_items.value < 2):
                # however, if theres not even enough for those 4, it just packs everything into one item. it's not like it'll bring a bunch of locations in logic at once, right?
                do_progression_instead = ["Can Research All Items"]
                if (totalChecksThere<1 or self.options.research_locked_behind_items.value == 0):
                    # and if it specifically doesn't want ANY researching locked, it does THIS.
                    do_progression_instead = []
            # then its removal time
            for num in range(0,len(removable_researchsanity_items)):
                if (removable_researchsanity_items[num].name in do_progression_instead):
                    self.useful_if_not_progression.update({removable_researchsanity_items[num].name})
                else:
                    items.remove(removable_researchsanity_items[num].name)
                    item_count -= 1
            rules_set_progression = -1*len(do_progression_instead)
        else:
            # but if there IS enough room, just go as usual.
            for rule in valid_rules:
                if rule.name in self.disabled_rules:
                    continue
    
                if invalid_flag(rule.flags, self.options) and rule.name not in goal_locations:
                    continue

                # disables these two items, as the other 18 items are present
                if rule.name == "Can Research All Items" or rule.name == "Can Research Stackable Items":
                    items.remove(rule.name)
                    item_count -= 1
                    continue

                if rule.flags.__contains__("ResearchItem"):
                    self.useful_if_not_progression.update({rule.name})
                    rules_set_progression += 1
                # make sure the item isn't removed by pointless_events,
                #if rule.flags.__contains__("ResearchGroup"):#rule.flags.__contains__("Research") or 
                #    mark_progression(rule.conditions)
                #    # then only TRULY mark the items as progression or useful.
        # useful for debugging how many items are removed by the above function -NonIdiot
        #logging.info("Terraria (Beta) says (Code NonIdiot005) "+str(totalChecksThere)+" "+str(self.options.research_locked_behind_items.value)+" "+str(rules_set_progression))

        def unreachable_rules(rule: Rule) -> Set[str]:
            if isinstance(rule, Has):
                assert isinstance(rule.item_name, str)
                if rule.item_name in bad_events:
                    return {rule.item_name}
                if rule.item_name not in events:
                    return set()
                if isinstance(self.ter_rules[rule.item_name], False_):
                    return {rule.item_name}
                sub_result = unreachable_rules(self.ter_rules[rule.item_name])
                return sub_result.union({rule.item_name}) if sub_result else set()
            elif isinstance(rule, And):
                result = set()
                for r in rule.children:
                    result = result.union(unreachable_rules(r))
                return result
            elif isinstance(rule, Or):
                result = set()
                for r in rule.children:
                    bad_flags = unreachable_rules(r)
                    if not bad_flags:
                        return set()
                    result = result.union(bad_flags)
                return result
            else:
                return set()

        for name in self.ter_rules:
            if name in events and unreachable_rules(self.ter_rules[name]):
                bad_events.add(name)

        bad_locs = set()
        bad_locs_shown = set()
        for loc in locations:
            if loc not in events:
                sub_result = unreachable_rules(self.ter_rules[loc])
                if sub_result:
                    bad_locs.add(loc)
                    bad_locs_shown.add(loc) # this is here in case i don't wanna spam the log with researchsanity-disabled stuff
                    bad_events = bad_events.union(sub_result)
        if bad_locs_shown:
            logging.warning(f"SLOT {slot_name}: The following locations are inaccessible due to your config and will be removed:\n"
                            f" - {'\n - '.join(bad_locs_shown)}")

        for flag in events_to_evaluate.keys():
            pool = events_to_evaluate[flag]
            for item in pool:
                if flag == "Pet" and unreachable_rules(self.ter_rules[item]):
                    bad_events.add(item)
                    continue
                self.progression.add(loc_to_item[item])
                rule = rules[rule_indices[item]]
                mark_progression(rule.operator, rule.conditions)

        location_count -= len(bad_locs)
        locations = [loc for loc in locations if (loc not in bad_locs and loc not in bad_events)]
        for loc in locations:
            mark_progression(rules[rule_indices[loc]].operator, rules[rule_indices[loc]].conditions)
        events = [i for i in events if i not in bad_events]
        items = [i for i in items if i not in bad_events]

        pointless_events = [event for event in events if (loc_to_item[event] not in self.progression and event not in self.useful_if_not_progression)]
        for event in pointless_events:
            locations.remove(event)
            items.remove(loc_to_item[event])
            location_count -= 1
            item_count -= 1

        ordered_rewards = [
            reward
            for reward in labels["ordered"]
            if ("Calamity" in self.options.mods.value or "Calamity" not in rewards[reward])
            and ("Fargo" in self.options.mods.value or "Fargo" not in rewards[reward])
        ]
        while (
                self.options.fill_extra_checks_with.value == 1
                and item_count < location_count
                and ordered_rewards
        ):
            items.append(ordered_rewards.pop(0))
            item_count += 1

        random_rewards = [
            reward
            for reward in labels["random"]
            if ("Calamity" in self.options.mods.value or "Calamity" not in rewards[reward])
            and ("Fargo" in self.options.mods.value or "Fargo" not in rewards[reward])
        ]
        # better to have this as self.random instead of self.multiworld.random -NonIdiot
        self.random.shuffle(random_rewards)
        while (
                self.options.fill_extra_checks_with.value == 1
                and item_count < location_count
                and random_rewards
        ):
            items.append(random_rewards.pop(0))
            item_count += 1

        while item_count < location_count:
            items.append("Reward: Coins")
            item_count += 1

        self.ter_items = items
        self.ter_locations = locations

        self.ter_goals = ter_goals
        self.goal_items = goal_items
        self.goal_locations = goal_locations

    def create_regions(self) -> None:
        menu = Region("Menu", self.player, self.multiworld)

        for location in self.ter_locations:
            rule = rules[rule_indices[location]]
            if "Npc" in rule.flags and not self.options.randomize_npcs.value:
                location_id = None
            else:
                location_id = location_name_to_id.get(location)

            menu.locations.append(
                TerrariaLocation(
                    self.player, location, location_id, menu
                )
            )

        self.multiworld.regions.append(menu)

    def create_item(self, item: str) -> TerrariaItem:
        if item in self.progression or item == self.OUT_OF_LOGIC:
            classification = ItemClassification.progression
        elif item in self.useful_if_not_progression:
            classification = ItemClassification.useful
        else:
            classification = ItemClassification.filler

        return TerrariaItem(item, classification, item_name_to_id[item], self.player)

    def create_items(self) -> None:
        for item in self.ter_items:
            if (rule_index := rule_indices.get(item)) is not None:
                rule = rules[rule_index]
                _, is_item = is_location_is_item(rule.flags, self.options)
                if "Item" in rule.flags:
                    if ("Research" in rule.flags or "ResearchItem" in rule.flags):
                        name = item
                    else:
                        name = rule.flags.get("Item") or f"Post-{item}"
                elif is_item:
                    name = item
                else:
                    continue
            else:
                name = item

            self.multiworld.itempool.append(self.create_item(name))

        locked_items = {}

        for location in self.ter_locations:
            rule = rules[rule_indices[location]]
            #if "Location" not in rule.flags and "Achievement" not in rule.flags \
            #        and not ("Research" in rule.flags and self.options.researchsanity.value) \
            #        and not ("Npc" in rule.flags and self.options.randomize_npcs.value):
            is_loc, _ = is_location_is_item(rule.flags, self.options)
            if not is_loc:
                if location in self.progression:
                    classification = ItemClassification.progression
                else:
                    classification = ItemClassification.useful

                locked_items[location] = TerrariaItem(
                    location, classification, None, self.player
                )

        for item, location in self.ter_goals.items():
            locked_items[location] = self.create_item(item)
        for location, item in locked_items.items():
            self.multiworld.get_location(location, self.player).place_locked_item(item)

    def create_rule_ini(
            self,
            operator: Union[bool, None],
            conditions: List[
                Tuple[
                    bool,
                    int,
                    Union[str, Tuple[Union[bool, None], list]],
                    Union[str, int, None],
                ]
            ]
    ) -> Rule:
        if operator is None:
            if len(conditions) == 0:
                return True_()
            if len(conditions) > 1:
                raise Exception("Found multiple conditions without an operator")
            cond = self.create_rule(conditions[0])
            if isinstance(cond, bool):
                return True_() if cond else False_()
            return cond
        sub_rules = [self.create_rule(condition) for condition in conditions]

        if operator:
            if any(isinstance(r, True_) for r in sub_rules):
                return True_()
            elif all(isinstance(r, False_) for r in sub_rules):
                return False_()
            final_rules = [r for r in sub_rules if not isinstance(r, False_)]
            return final_rules[0] if len(final_rules) == 1 else Or(*final_rules)
        else:
            if any(isinstance(r, False_) for r in sub_rules):
                return False_()
            elif all(isinstance(r, True_) for r in sub_rules):
                return True_()
            final_rules = [r for r in sub_rules if not isinstance(r, True_)]
            return final_rules[0] if len(final_rules) == 1 else And(*final_rules)

    def create_rule(self, condition: Condition) -> Rule:
        if condition.type == COND_ITEM:
            if condition.condition in self.disabled_rules:
                return False_()
            
            rule = rules[rule_indices[condition.condition]]
            if "Item" in rule.flags:
                if ("Research" in rule.flags or "ResearchItem" in rule.flags):
                    name = condition.condition
                else:
                    name = rule.flags.get("Item") or f"Post-{condition.condition}"
            else:
                name = condition.condition

            assert(isinstance(name, str))
            return Has(name)
        elif condition.type == COND_LOC:
            if condition.condition in self.disabled_rules:
                return False_()
            
            rule = rules[rule_indices[condition.condition]]
            return self.create_rule_ini(rule.operator, rule.conditions)
        elif condition.type == COND_FN:
            def bool_to_rule(b):
                return True_() if b else False_()
            match condition.condition:
                case "npc":
                    assert(isinstance(condition.argument, int))
                    return HasFromList(*npcs, count=condition.argument)
                case "npc_rando":
                    return bool_to_rule(not (condition.sign ^ self.options.randomize_npcs.value))
                case "depth":
                    assert(isinstance(condition.argument, int))
                    return Has("Progressive Depth", count=condition.argument,
                               options=[OptionFilter(RandomizeDepth, 1, "ge")],
                               filtered_resolution=True)
                case "depth_safety":
                    return bool_to_rule(not (condition.sign ^ (self.options.randomize_depth.value > 1)))
                case "calamity":
                    return bool_to_rule(not (condition.sign ^ ("Calamity" in self.options.mods.value)))
                case "fargo":
                    return bool_to_rule(not (condition.sign ^ ("Fargo" in self.options.mods.value)))
                case "rare":
                    return bool_to_rule(not (condition.sign ^ self.options.rare_achievements.value))
                case "time":
                    return bool_to_rule(not (condition.sign ^ self.options.time_achievements.value))
                case "pickaxe":
                    if type(condition.argument) is not int:
                        raise Exception("@pickaxe requires an integer argument")

                    eligible_items = []
                    for pickaxe, power in pickaxes.items():
                        if power >= condition.argument:
                            eligible_items.append(pickaxe)

                    return HasAny(*eligible_items)
                case "hammer":
                    if type(condition.argument) is not int:
                        raise Exception("@hammer requires an integer argument")

                    eligible_items = []
                    for hammer, power in hammers.items():
                        if power >= condition.argument:
                            eligible_items.append(hammer)

                    return HasAny(*eligible_items)
                case "mech_boss":
                    assert(isinstance(condition.argument, int))
                    return HasFromList(*mech_bosses, count=condition.argument)
                case "minions":
                    assert (isinstance(condition.argument, int))
                    return HasMinion(condition.argument)
                case "health":
                    if type(condition.argument) is not int:
                        raise Exception("@health requires an integer argument")

                    if not self.options.health_logic.value:
                        return True_()

                    health_required = max(condition.argument + self.options.health_logic_handicap.value, 1)

                    if health_required == 1:
                        health_rule = Has(health_upgrades[0])
                    elif health_required == 2:
                        health_rule = HasAll(*health_upgrades[:2])
                    else:
                        highest_base = HasAll(*health_upgrades[:2])
                        quarter_fruits_required = health_required - 2
                        quarter_fruits_rule = HasFromList(
                            *quarter_fruits,
                            count=quarter_fruits_required,
                            options=[OptionFilter(Mods, "Calamity", operator="contains")],
                            filtered_resolution=True
                        )
                        health_rule = highest_base & quarter_fruits_rule

                    # Out of logic will be shown as Glitched Logic.
                    if self.is_ut:
                        return Or(health_rule, Has(self.OUT_OF_LOGIC))

                    return Filtered(health_rule, filtered_resolution=True)
                case "getfixedboi":
                    return bool_to_rule(not (condition.sign ^ self.options.getfixedboi.value))
                case "shimmer_skips":
                    return bool_to_rule(not (condition.sign ^ self.options.shimmer_skips.value))
                case "fishing":
                    return bool_to_rule(not (condition.sign ^ self.options.fishing_logic.value))
                case "researchsanity":
                    if condition.sign:
                        # SHOULD return false if Researchsanity==0
                        return False_(options=[OptionFilter(Researchsanity, 0)])
                    else:
                        return True_(options=[OptionFilter(Researchsanity, 0)])
                case "research_items":
                    if condition.sign:
                    # SHOULD return false if ResearchLockedBehindItems==0
                        return False_(options=[OptionFilter(ResearchLockedBehindItems, 0)])
                    else:
                        return True_(options=[OptionFilter(ResearchLockedBehindItems, 0)])
                case _:
                    raise Exception(f"Unknown function {condition.condition}")
        elif condition.type == COND_GROUP:
            operator, conditions = condition.condition

            return self.create_rule_ini(operator, conditions)

    def set_rules(self) -> None:
        for location_name in self.ter_locations:
            location = self.multiworld.get_location(location_name, self.player)
            rule = rules[rule_indices[location_name]]
            created_rule = self.create_rule_ini(rule.operator, rule.conditions)
            self.set_rule(location, created_rule)
        
        # UT won't recognize you're in go mode because it checks if you have the Post-Boss flags.
        # To fix this we check if we can reach the goal locations.
        if self.is_ut:
            self.set_completion_rule(lambda state: all(state.can_reach_location(loc, self.player) for loc in self.goal_locations))
        else:
            self.set_completion_rule(HasAll(*self.goal_items))

    def fill_slot_data(self) -> Dict[str, object]:
        return {
            "goal": list(self.goal_locations),
            "deathlink": bool(self.options.death_link),
            "version": list(self.required_client_version),
            "mods": list(self.options.mods.value),
            "calamity": int("Calamity" in self.options.mods.value),
            "fargo": int("Fargo" in self.options.mods.value),
            "getfixedboi": self.options.getfixedboi.value,
            "npc_rando": self.options.randomize_npcs.value,
            "depth_rando": self.options.randomize_depth.value,
            "randomize_npcs": list(self.npcs_to_randomize),
            "goal_option": self.options.goal.value,
            "shuffle_to_option": self.options.shuffle_to.value,
            "early_achievements": self.options.early_achievements.value,
            "normal_achievements": self.options.normal_achievements.value,
            "rare_achievements": self.options.rare_achievements.value,
            "time_achievements": self.options.time_achievements.value,
            "crafting_achievements": self.options.crafting_achievements.value,
            "grindy_achievements": self.options.grindy_achievements.value,
            "fishing_achievements": self.options.fishing_achievements.value,
            "fishing_logic": self.options.fishing_logic.value,
            "researchsanity": self.options.researchsanity.value,
            "researchsanity_seed": self.options.researchsanity_seed.value,
            "research_locked_behind_items": self.options.research_locked_behind_items.value,
            "fill_extra_checks_with": self.options.fill_extra_checks_with.value,
            "shimmer_skips": self.options.shimmer_skips.value,
            "health_logic": self.options.health_logic.value,
            "health_logic_handicap": self.options.health_logic_handicap.value,
        }
        
    def shuffle_with_seed(self, array: list, seed: int = None) -> list:
        if seed is None:
            seed = random.randint(1, 0xFFFE)

        randome = ThreadBarrierProxy(random.Random(seed))
        shuffled = array
        randome.shuffle(array)
        return shuffled

        #length_ = len(array)
        #shuffled = [0]*length_
        #seed_pos = seed ^ 0xFFFF

        #for i in list(reversed(range(length_))):
        #    index = seed_pos % (i + 1)
        #    shuffled[i] = array.pop(index)

        #return shuffled

@dataclasses.dataclass()
class HasMinion(Rule["TerrariaWorld"], game=TerrariaWorld.game):

    target: int

    @override
    def _instantiate(self, world: TWorld) -> Rule.Resolved:
        return self.Resolved(self.target, player=world.player, caching_enabled=True)

    class Resolved(Rule.Resolved):
        target: int

        @override
        def _evaluate(self, state: CollectionState) -> bool:
            count = 1
            for armor, minion in armor_minions:
                if state.has(armor, self.player):
                    count += minion
                    break
            if count >= self.target:
                return True
            for accessory, minion in accessory_minions:
                if state.has(accessory, self.player):
                    count += minion
                    if count >= self.target:
                        return True
            return False

        @override
        def item_dependencies(self) -> dict[str, set[int]]:
            item_dict = {}
            for item in armor_minions + accessory_minions:
                item_dict[item[0]] = {id(self)}
            return item_dict