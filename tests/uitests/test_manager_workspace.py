# This work is licensed under the GNU GPLv2 or later.
# See the COPYING file in the top-level directory.

import pyatspi

from . import lib


def _filter(app, name):
    return app.get_manager().find("group-list").find(name, "list item")


def _name_group(app, title, name):
    dialog = app.find_window(title)
    dialog.find("group-name-entry", "text").set_text(name)
    dialog.find("OK", "push button").click()
    lib.utils.check(lambda: not dialog.showing)


def testManagerGroups(app):
    manager = app.topwin
    manager.find("sidebar-new-group", "push button").click()
    _name_group(app, "New Group", "Workspace test")
    _filter(app, r"Workspace test \(0\)")

    _filter(app, "All machines").click()
    cell = manager.find("test-clone-simple$", "table cell")
    cell.queryComponent().grabFocus()
    app.rawinput.pressKey("Menu")
    menu = app.root.find("vm-action-menu")
    menu.find("Group", "menu").point()
    menu.find("Group", "menu").find("Workspace test", "check menu item").click()
    _filter(app, r"Workspace test \(1\)").click()
    manager.find("test-clone-simple$", "table cell").click()
    lib.utils.check(lambda: manager.find("card-value-group").text == "Workspace test")

    # Rename a populated group, retaining its VM membership.
    _filter(app, "Workspace test").click(button=3)
    app.root.find("group-menu").find("group-rename", "menu item").click()
    _name_group(app, "Rename Group", "Renamed workspace")
    _filter(app, r"Renamed workspace \(1\)")
    manager.find("test-clone-simple$", "table cell")

    # The 'All machines' view lists the VMs under a heading per group,
    # with the group also shown in its own column
    _filter(app, "All machines").click()
    table = manager.find("vm-list")
    heading = table.find(".*Renamed workspace.*1 machine.*", "table cell")
    cell = table.find("test-clone-simple$", "table cell")
    lib.utils.check(lambda: cell.showing)
    table.find("Group", "table column header")

    def _vm_listed():
        return bool(
            table.findChildren(
                lambda node: node.roleName == "table cell" and node.name == "test-clone-simple",
                isLambda=True,
            )
        )

    # Clicking a heading collapses the group, clicking again expands it
    heading.queryComponent().scrollTo(pyatspi.SCROLL_ANYWHERE)
    heading.click()
    lib.utils.check(lambda: not _vm_listed())
    heading = table.find(".*Renamed workspace.*1 machine.*", "table cell")
    heading.queryComponent().scrollTo(pyatspi.SCROLL_ANYWHERE)
    heading.click()
    lib.utils.check(_vm_listed)

    # A single group's view is a flat table without headings
    _filter(app, "Renamed workspace").click()
    lib.utils.check(
        lambda: not table.findChildren(
            lambda node: node.roleName == "table cell" and "machine" in node.name,
            isLambda=True,
        )
    )

    # Deleting a group keeps its machine, now in the ungrouped filter.
    _filter(app, "Renamed workspace").click(button=3)
    app.root.find("group-menu").find("group-ungroup", "menu item").click()
    _filter(app, "Ungrouped").click()
    cell = manager.find("test-clone-simple$", "table cell")
    cell.click()
    lib.utils.check(lambda: manager.find("card-value-group").text == "Ungrouped")


def testManagerCardActions(app):
    manager = app.topwin
    cell = manager.find("test-clone-simple$", "table cell")
    cell.click()
    run = manager.find("card-run", "push button")
    pause = manager.find("card-pause", "push button")
    run.click()
    lib.utils.check(lambda: not run.showing)
    pause.click()
    lib.utils.check(lambda: "Paused" in manager.find("card-value-status").text)
    pause.click()
    lib.utils.check(lambda: "Running" in manager.find("card-value-status").text)
    manager.find("card-details", "push button").click()
    app.find_details_window("test-clone-simple", click_details=True).window_close()

    # A second click on a selected VM dismisses its card.
    cell.queryComponent().scrollTo(pyatspi.SCROLL_ANYWHERE)
    cell.click()
    lib.utils.check(lambda: not run.showing)


def testManagerMouseMenu(app):
    manager = app.topwin
    # Keep the target row visible when selecting it reveals the card.
    manager.window_maximize()
    cell = manager.find("test-clone-simple$", "table cell")
    for _ in range(2):
        cell.click(button=3)
        menu = app.root.find("vm-action-menu")
        menu.find("Clone", "menu item").click()
        win = app.find_window("Clone Virtual Machine")
        win.find("Cancel", "push button").click()
        lib.utils.check(lambda: not win.showing)


def testManagerFavorites(app):
    manager = app.topwin
    manager.window_maximize()
    lib.utils.check(lambda: _filter(app, r"Favorites \(0\)").showing)

    # Add from the context menu
    manager.find("test-clone-simple$", "table cell").click(button=3)
    menu = app.root.find("vm-action-menu")
    menu.find("Add to Favorites", "menu item").click()
    _filter(app, r"Favorites \(1\)")

    # Add from the card
    manager.find("test-clone$", "table cell").click()
    star = manager.find("card-favorite", "push button")
    star.click()
    _filter(app, r"Favorites \(2\)").click()
    vmlist = manager.find("vm-list")
    lib.utils.check(lambda: len(vmlist.findChildren(_is_name_cell)) == 2)

    # Remove again, the machine leaves the view
    cell = manager.find("test-clone-simple.*", "table cell")
    cell.click(button=3)
    menu = app.root.find("vm-action-menu")
    menu.find("Remove from Favorites", "menu item").click()
    _filter(app, r"Favorites \(1\)")
    lib.utils.check(lambda: len(vmlist.findChildren(_is_name_cell)) == 1)


def _is_name_cell(node):
    return node.roleName == "table cell" and node.name.startswith("test")


def testManagerSearch(app):
    manager = app.topwin
    vmlist = manager.find("vm-list")
    search = manager.find("vm-search", "text")
    total = len(vmlist.findChildren(_is_name_cell))

    search.set_text("CLONE")
    lib.utils.check(lambda: len(vmlist.findChildren(_is_name_cell)) == 3)
    manager.find("test-clone-full$", "table cell")

    search.set_text("no such machine")
    lib.utils.check(lambda: len(vmlist.findChildren(_is_name_cell)) == 0)

    # Escape clears the search
    search.click()
    app.rawinput.pressKey("Escape")
    lib.utils.check(lambda: search.text == "")
    lib.utils.check(lambda: len(vmlist.findChildren(_is_name_cell)) == total)

    # Ctrl+F jumps to the search box
    manager.find("test-clone$", "table cell").click()
    manager.keyCombo("<ctrl>f")
    lib.utils.check(lambda: search.focused)
