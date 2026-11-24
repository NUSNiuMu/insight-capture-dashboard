function point(temperature, pwm) {
    printf "\t\t%d 0 %d 0\n", temperature, pwm
}

$1 == "<FAN" {
    fans++
    use_margin = group_max = has_cont = 0
}
$1 == "TMARGIN" { use_margin = ($2 == "ENABLED") }
$1 == "GROUP_MAX_TEMP" { group_max = $2 + 0 }
$1 == "FAN_GOVERNOR" && $2 == "cont" { has_cont = 1 }

$1 == "FAN_DEFAULT_CONTROL" {
    print "\tFAN_DEFAULT_CONTROL open_loop"
    controls++
    next
}
$1 == "FAN_DEFAULT_PROFILE" {
    print "\tFAN_PROFILE balanced {"
    if (use_margin) {
        # TMARGIN is the group reference temperature minus measured temperature.
        if (group_max <= 75) exit 1
        point(0, 255)
        point(group_max - 75, 255)
        point(group_max - 65, 220)
        point(group_max - 55, 180)
        point(group_max - 45, 145)
        point(group_max, 145)
    } else {
        point(0, 145)
        point(45, 145)
        point(55, 180)
        point(65, 220)
        point(75, 255)
        point(105, 255)
    }
    print "\t}"
    print "\tFAN_DEFAULT_PROFILE balanced"
    profiles++
    next
}
$1 == "FAN_DEFAULT_GOVERNOR" {
    if (!has_cont) {
        print "\tFAN_GOVERNOR cont {"
        print "\t\tSTEP_SIZE 10"
        print "\t}"
    }
    print "\tFAN_DEFAULT_GOVERNOR cont"
    governors++
    next
}
{ print }
END { if (!fans || profiles != fans || controls != fans || governors != fans) exit 1 }
