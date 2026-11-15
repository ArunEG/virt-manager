# Copyright (C) 2006-2008, 2013-2014 Red Hat, Inc.
# Copyright (C) 2006 Daniel P. Berrange <berrange@redhat.com>
#
# This work is licensed under the GNU GPLv2 or later.
# See the COPYING file in the top-level directory.

import math

import cairo

from gi.repository import Gtk
from gi.repository import Gdk
from gi.repository import Pango

from virtinst import log
from virtinst import xmlutil

from . import vmmenu
from .lib import uiutil
from .asyncjob import vmmAsyncJob
from .baseclass import vmmGObjectUI
from .connmanager import vmmConnectionManager
from .engine import vmmEngine
from .lib.graphwidgets import CellRendererIconTile
from .lib.graphwidgets import CellRendererMemBar
from .lib.graphwidgets import CellRendererSparkline
from .lib.graphwidgets import CellRendererStatusPill

# Number of data points for performance graphs
GRAPH_LEN = 40

# fields in the VM list model data set
(
    ROW_HANDLE,
    ROW_SORT_KEY,
    ROW_MARKUP,
    ROW_HINT,
    ROW_STATUS_TEXT,
    ROW_STATUS_COLOR,
    ROW_IS_VM,
    ROW_GROUP,
    ROW_BG,
    ROW_INDENT,
    ROW_GROUP_TEXT,
) = range(11)

# Tint of the group heading rows. Translucent, so it suits any theme
BG_GROUP_HEADING = "rgba(128,128,128,0.09)"

# Sort ids for the VM list columns
(
    COL_NAME,
    COL_GUEST_CPU,
    COL_HOST_CPU,
    COL_MEM,
    COL_DISK,
    COL_NETWORK,
    COL_STATUS,
    COL_MEMSIZE,
    COL_GROUP,
) = range(9)

# How far VM rows are indented under their group heading
GROUP_ROW_INDENT = 26

COLOR_RUNNING = uiutil.COLOR_RUNNING
COLOR_PAUSED = uiutil.COLOR_PAUSED
COLOR_OFF = uiutil.COLOR_OFF
_dot_markup = uiutil.dot_markup
_vm_status_color = uiutil.vm_status_color

# Sidebar filters: which VMs of the current connection are listed
FILTER_ALL = ("all", None)
FILTER_UNGROUPED = ("ungrouped", None)

MANAGER_MIN_DEFAULT_SIZE = (1120, 680)


def _filter_group(groupname):
    return ("group", groupname)


def _cmp(a, b):
    return (a > b) - (a < b)


def _vm_memory_kib(vm):
    """
    Memory the VM is currently using: the polled value if memory stats
    are enabled, otherwise its current allocation. 0 if not running
    """
    if not vm.is_active():
        return 0
    return vm.stats_memory() or vm.get_xmlobj().currentMemory or 0


def _pretty_memory(kib):
    if kib >= 1024 * 1024:
        return "%s GB" % ("%.1f" % (kib / (1024.0 * 1024.0))).replace(".0", "")
    return "%d MB" % (kib / 1024.0)


class _VMGroupMenu(Gtk.Menu):
    """
    'Group' submenu of the manager VM context menu. Rebuilt on every
    popup, since the list of groups is dynamic
    """

    def __init__(self, manager):
        Gtk.Menu.__init__(self)
        self._manager = manager
        self.get_accessible().set_name("vm-group-menu")

    def update_widget_states(self, vm):
        for child in self.get_children():
            self.remove(child)
            child.destroy()
        if not vm:
            return

        manager = self._manager
        current = vm.get_group()

        for name in manager.list_group_names(vm.conn):
            item = Gtk.CheckMenuItem.new_with_label(name)
            item.set_draw_as_radio(True)
            item.set_active(name == current)
            item.connect("activate", lambda _src, n=name: manager.set_vm_group(vm, n))
            self.add(item)
        if self.get_children():
            self.add(Gtk.SeparatorMenuItem())

        item = Gtk.MenuItem.new_with_mnemonic(_("_New Group..."))
        item.connect("activate", lambda _src: manager.set_vm_new_group(vm))
        self.add(item)

        item = Gtk.MenuItem.new_with_mnemonic(_("_Remove from Group"))
        item.set_sensitive(bool(current))
        item.connect("activate", lambda _src: manager.set_vm_group(vm, ""))
        self.add(item)
        self.show_all()


def _vm_os_label(vm):
    """
    Name of the operating system the VM is configured for
    """
    osinfo = vm.xmlobj.osinfo
    if not osinfo or osinfo.is_generic():
        return _("Unknown")
    return osinfo.label


def _vm_network_label(vm):
    """
    Short list of what the VM's network interfaces are attached to
    """
    names = []
    for net in vm.xmlobj.devices.interface:
        name = net.source or net.type or ""
        if net.type == "user":
            name = _("Usermode")
        if name and name not in names:
            names.append(name)
    return ", ".join(names) or _("None")


class vmmManager(vmmGObjectUI):
    @classmethod
    def get_instance(cls, parentobj):
        try:
            if not cls._instance:
                cls._instance = vmmManager()
            return cls._instance
        except Exception as e:  # pragma: no cover
            if not parentobj:
                raise
            parentobj.err.show_err(_("Error launching manager: %s") % str(e))

    def __init__(self):
        vmmGObjectUI.__init__(self, "manager.ui", "vmm-manager")
        self._cleanup_on_app_close()

        w, h = self.config.get_manager_window_size()
        minw, minh = MANAGER_MIN_DEFAULT_SIZE
        self.topwin.set_default_size(max(w or 0, minw), max(h or 0, minh))
        self.prev_position = None
        self._window_size = None

        self.vmmenu = vmmenu.VMActionMenu(self, self.current_vm)
        self.shutdownmenu = vmmenu.VMShutdownMenu(self, self.current_vm)
        self.connmenu = Gtk.Menu()
        self.connmenu.get_accessible().set_name("conn-menu")
        self.connmenu_items = {}
        self.groupmenu = Gtk.Menu()
        self.groupmenu.get_accessible().set_name("group-menu")
        self.groupmenu_items = {}

        self.builder.connect_signals(
            {
                "on_menu_view_guest_cpu_usage_activate": self.toggle_stats_visible_guest_cpu,
                "on_menu_view_host_cpu_usage_activate": self.toggle_stats_visible_host_cpu,
                "on_menu_view_memory_usage_activate": self.toggle_stats_visible_memory_usage,
                "on_menu_view_disk_io_activate": self.toggle_stats_visible_disk,
                "on_menu_view_network_traffic_activate": self.toggle_stats_visible_network,
                "on_vm_manager_delete_event": self.close,
                "on_vmm_manager_configure_event": self.window_resized,
                "on_menu_file_add_connection_activate": self.open_newconn,
                "on_menu_new_vm_activate": self.new_vm,
                "on_menu_new_group_activate": self.new_group,
                "on_menu_file_quit_activate": self.exit_app,
                "on_menu_file_close_activate": self.close,
                "on_vmm_close_clicked": self.close,
                "on_vm_open_clicked": self.show_vm,
                "on_vm_run_clicked": self.start_vm,
                "on_vm_new_clicked": self.new_vm,
                "on_vm_shutdown_clicked": self.poweroff_vm,
                "on_vm_pause_clicked": self.pause_vm_button,
                "on_menu_edit_details_activate": self.show_vm,
                "on_menu_edit_delete_activate": self.do_delete,
                "on_menu_host_details_activate": self.show_host,
                "on_vm_list_row_activated": self.row_activated,
                "on_vm_list_button_press_event": self.popup_vm_menu_button,
                "on_vm_list_key_press_event": self.popup_vm_menu_key,
                "on_menu_edit_preferences_activate": self.show_preferences,
                "on_menu_help_about_activate": self.show_about,
            }
        )

        # Sidebar state: the connection whose VMs are listed, and which
        # of them (all, ungrouped, or one group)
        self._conns = []
        self._current_conn = None
        self._filter = FILTER_ALL
        self._sidebar_updating = False

        self._conn_list = None
        self._filter_list = None
        self._title_label = None
        self._summary_label = None
        self._create_button = None
        self._group_col = None
        self._collapsed_groups = set()
        self._toggle_off_path = None
        self._detail_revealer = None
        self._detail_name = None
        self._detail_buttons = None
        self._detail_values = None

        # There seem to be ref counting issues with calling
        # list.get_column, so avoid it
        self.diskcol = None
        self.netcol = None
        self.memcol = None
        self.guestcpucol = None
        self.hostcpucol = None
        self.spacer_txt = None
        self.init_layout()
        self.init_vmlist()

        self.init_stats()
        self.init_toolbar()
        self.init_context_menus()

        self.update_current_selection()
        self.widget("vm-list").get_selection().connect("changed", self.update_current_selection)

        self.max_disk_rate = 10.0
        self.max_net_rate = 10.0

        # Initialize stat polling columns based on global polling
        # preferences (we want signal handlers for this)
        self._config_polling_change_cb(COL_GUEST_CPU)
        self._config_polling_change_cb(COL_DISK)
        self._config_polling_change_cb(COL_NETWORK)
        self._config_polling_change_cb(COL_MEM)

        connmanager = vmmConnectionManager.get_instance()
        connmanager.connect("conn-added", self._conn_added)
        connmanager.connect("conn-removed", self._conn_removed)
        for conn in connmanager.conns.values():
            self._conn_added(connmanager, conn)

    ##################
    # Common methods #
    ##################

    def show(self):
        vis = self.is_visible()
        self.topwin.present()
        if vis:
            return

        log.debug("Showing manager")
        if self.prev_position:
            self.topwin.move(*self.prev_position)
            self.prev_position = None

        vmmEngine.get_instance().increment_window_counter()

    def close(self, src_ignore=None, src2_ignore=None):
        if not self.is_visible():
            return

        log.debug("Closing manager")
        self.prev_position = self.topwin.get_position()
        self.topwin.hide()
        vmmEngine.get_instance().decrement_window_counter()

        return 1

    def _cleanup(self):
        self.diskcol = None
        self.guestcpucol = None
        self.memcol = None
        self.hostcpucol = None
        self.netcol = None

        self._conns = None
        self._current_conn = None
        self._conn_list = None
        self._filter_list = None

        self.shutdownmenu.destroy()
        self.shutdownmenu = None
        self.vmmenu.destroy()
        self.vmmenu = None
        self.connmenu.destroy()
        self.connmenu = None
        self.connmenu_items = None
        self.groupmenu.destroy()
        self.groupmenu = None
        self.groupmenu_items = None

        if self._window_size:
            self.config.set_manager_window_size(*self._window_size)

    def set_startup_error(self, msg):
        self.widget("vm-notebook").set_current_page(1)
        self.widget("startup-error-label").set_text(msg)

    ################
    # Init methods #
    ################

    def init_layout(self):
        """
        Rearrange the glade layout into a sidebar (connections, and the
        groups of the selected connection) next to the machine table
        """
        notebook = self.widget("vm-notebook")
        notebook.set_show_tabs(False)
        notebook.set_show_border(False)
        scroll = self.widget("scrolledwindow1")
        notebook.remove(scroll)

        def section_label(text):
            label = Gtk.Label(label=text, xalign=0)
            label.get_style_context().add_class("vmm-section-label")
            label.set_margin_start(22)
            label.set_margin_top(18)
            label.set_margin_bottom(6)
            return label

        # Sidebar
        # Everything sits on one painted backdrop
        hbox = Gtk.Box()
        hbox.connect("draw", self._backdrop_draw_cb)

        sidebar = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        sidebar.get_style_context().add_class("vmm-sidebar")
        sidebar.set_size_request(240, -1)

        self._conn_list = Gtk.ListBox()
        self._conn_list.set_activate_on_single_click(False)
        self._conn_list.get_accessible().set_name("conn-list")
        self._conn_list.connect("row-selected", self._conn_row_selected_cb)
        self._conn_list.connect("row-activated", self._conn_row_activated_cb)
        self._conn_list.connect("button-press-event", self._conn_list_button_press_cb)

        self._filter_list = Gtk.ListBox()
        self._filter_list.get_style_context().add_class("vmm-filter-list")
        self._filter_list.get_accessible().set_name("group-list")
        self._filter_list.connect("row-selected", self._filter_row_selected_cb)
        self._filter_list.connect("button-press-event", self._filter_list_button_press_cb)

        new_group = Gtk.Button()
        new_group.set_relief(Gtk.ReliefStyle.NONE)
        new_group.get_style_context().add_class("vmm-side-button")
        box = Gtk.Box(spacing=10)
        box.add(Gtk.Image.new_from_icon_name("list-add-symbolic", Gtk.IconSize.MENU))
        box.add(Gtk.Label(label=_("New group")))
        new_group.add(box)
        new_group.connect("clicked", self.new_group)
        new_group.get_accessible().set_name("sidebar-new-group")
        self._new_group_button = new_group

        filter_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        filter_box.add(self._filter_list)
        filter_box.add(new_group)
        filter_scroll = Gtk.ScrolledWindow()
        filter_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        filter_scroll.add(filter_box)

        sidebar.pack_start(section_label(_("CONNECTION")), False, False, 0)
        sidebar.pack_start(self._conn_list, False, False, 0)
        sidebar.pack_start(section_label(_("WORKSPACE")), False, False, 0)
        sidebar.pack_start(filter_scroll, True, True, 0)

        # Main panel: title, summary and actions above the machine table
        main = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        main.get_style_context().add_class("vmm-main")

        self._title_label = Gtk.Label(xalign=0)
        self._title_label.get_style_context().add_class("vmm-title")
        self._title_label.set_ellipsize(Pango.EllipsizeMode.END)
        self._summary_label = Gtk.Label(xalign=0)
        titles = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        titles.add(self._title_label)
        titles.add(self._summary_label)

        self._create_button = Gtk.Button()
        self._create_button.get_style_context().add_class("vmm-create")
        box = Gtk.Box(spacing=8)
        box.add(Gtk.Image.new_from_icon_name("list-add-symbolic", Gtk.IconSize.MENU))
        box.add(Gtk.Label(label=_("Create VM")))
        self._create_button.add(box)
        self._create_button.set_valign(Gtk.Align.CENTER)
        self._create_button.connect("clicked", self.new_vm)
        self._create_button.get_accessible().set_name("create-vm")

        # The run/pause/shutdown toolbar moves next to the create button
        toolbar = self.widget("vm-toolbar")
        toolbar.get_parent().remove(toolbar)
        toolbar.set_valign(Gtk.Align.CENTER)
        hidden = [self.widget("vm-new")]
        hidden += [c for c in toolbar.get_children() if isinstance(c, Gtk.SeparatorToolItem)]
        for child in hidden:
            child.set_no_show_all(True)
            child.hide()

        header = Gtk.Box(spacing=12)
        header.set_margin_bottom(14)
        header.pack_start(titles, True, True, 0)
        header.pack_end(self._create_button, False, False, 0)
        header.pack_end(toolbar, False, False, 0)

        scroll.set_shadow_type(Gtk.ShadowType.NONE)
        scroll.get_style_context().add_class("vmm-card")
        scroll.connect_after("draw", self._card_draw_cb, hbox)
        self.topwin.connect("button-press-event", self._window_button_press_cb)
        main.pack_start(header, False, False, 0)
        main.pack_start(scroll, True, True, 0)
        main.pack_start(self._build_detail_card(), False, False, 0)

        hbox.pack_start(sidebar, False, False, 0)
        hbox.pack_start(main, True, True, 0)
        hbox.show_all()
        notebook.insert_page(hbox, None, 0)
        notebook.set_current_page(0)

    def _build_detail_card(self):
        """
        Card under the machine table that slides open for the selected
        VM, with its key facts and the most common actions
        """
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=16)
        card.get_style_context().add_class("vmm-detail-card")
        # The gap above belongs to the card, so that a closed card leaves
        # the table and the sidebar ending on the same line
        card.set_margin_top(14)

        self._detail_name = Gtk.Label(xalign=0)
        self._detail_name.get_style_context().add_class("vmm-detail-name")
        self._detail_name.set_ellipsize(Pango.EllipsizeMode.END)

        def make_button(text, cb, icon_name, classname, primary=False):
            button = Gtk.Button()
            button.get_style_context().add_class(classname)
            box = Gtk.Box(spacing=8)
            image = Gtk.Image.new_from_icon_name(icon_name, Gtk.IconSize.MENU)
            box.add(image)
            label = Gtk.Label(label=text)
            box.add(label)
            button.add(box)
            button.vmm_label = label
            button.vmm_image = image
            if primary:
                button.get_style_context().add_class("suggested-action")
            button.connect("clicked", cb)
            return button

        def name_buttons():
            # Distinct accessible names: the toolbar has buttons with the
            # same labels
            for name, button in self._detail_buttons.items():
                button.get_accessible().set_name("card-" + name)

        self._detail_buttons = {
            "console": make_button(
                _("Console"),
                self.show_vm,
                "utilities-terminal-symbolic",
                "vmm-act-open",
                primary=True,
            ),
            "details": make_button(
                _("Details"),
                self._detail_details_cb,
                "emblem-system-symbolic",
                "vmm-act-details",
            ),
            "run": make_button(
                _("Run"), self.start_vm, "media-playback-start-symbolic", "vmm-act-run"
            ),
            "pause": make_button(
                _("Pause"),
                self._detail_pause_cb,
                "media-playback-pause-symbolic",
                "vmm-act-pause",
            ),
            "shutdown": make_button(
                _("Shut down"), self.poweroff_vm, "system-shutdown-symbolic", "vmm-act-stop"
            ),
        }

        name_buttons()

        top = Gtk.Box(spacing=10)
        top.add(Gtk.Image.new_from_icon_name("computer-symbolic", Gtk.IconSize.LARGE_TOOLBAR))
        top.pack_start(self._detail_name, True, True, 0)
        for name in ["shutdown", "pause", "run", "details", "console"]:
            top.pack_end(self._detail_buttons[name], False, False, 0)

        facts = Gtk.Box(homogeneous=True, spacing=24)
        self._detail_values = {}
        for key, title in [
            ("vcpus", _("Virtual CPUs")),
            ("memory", _("Allocated memory")),
            ("os", _("Operating system")),
            ("network", _("Network")),
            ("group", _("Group")),
            ("status", _("Status")),
        ]:
            title_label = Gtk.Label(label=title, xalign=0)
            title_label.get_style_context().add_class("vmm-dim")
            value_label = Gtk.Label(xalign=0)
            value_label.set_ellipsize(Pango.EllipsizeMode.END)
            value_label.set_width_chars(8)
            value_label.get_accessible().set_name("card-value-" + key)
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
            box.add(title_label)
            box.add(value_label)
            facts.add(box)
            self._detail_values[key] = value_label

        card.add(top)
        card.add(Gtk.Separator())
        card.add(facts)

        self._detail_revealer = Gtk.Revealer()
        self._detail_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_UP)
        self._detail_revealer.add(card)
        return self._detail_revealer

    def _detail_details_cb(self, _src):
        from .vmwindow import vmmVMWindow

        vm = self.current_vm()
        if vm:
            vmmVMWindow.get_instance(self, vm).show_details_page()

    def _detail_pause_cb(self, _src):
        vm = self.current_vm()
        if not vm:
            return  # pragma: no cover
        if vm.is_paused():
            vmmenu.VMActionUI.resume(self, vm)
        else:
            vmmenu.VMActionUI.suspend(self, vm)

    def _refresh_detail_card(self, vm):
        self._detail_revealer.set_reveal_child(bool(vm))
        if not vm:
            return

        xmlobj = vm.get_xmlobj()
        vcpus = xmlobj.vcpus or 1
        self._detail_name.set_text(vm.get_name_or_title())
        self._detail_values["vcpus"].set_text(ngettext("%d vCPU", "%d vCPUs", vcpus) % vcpus)
        self._detail_values["memory"].set_text(_pretty_memory(xmlobj.memory or 0))
        self._detail_values["group"].set_text(vm.get_group() or _("Ungrouped"))
        for key, text in [("os", _vm_os_label(vm)), ("network", _vm_network_label(vm))]:
            self._detail_values[key].set_text(text)
            self._detail_values[key].set_tooltip_text(text)
        self._detail_values["status"].set_markup(
            _dot_markup(_vm_status_color(vm), vm.run_status())
        )

        paused = vm.is_paused()
        can_pause = paused and vm.is_unpauseable() or vm.is_pauseable()
        buttons = self._detail_buttons
        buttons["run"].set_visible(vm.is_runable())
        buttons["pause"].set_visible(bool(can_pause))
        buttons["pause"].vmm_label.set_text(paused and _("Resume") or _("Pause"))
        buttons["pause"].vmm_image.set_from_icon_name(
            paused and "media-playback-start-symbolic" or "media-playback-pause-symbolic",
            Gtk.IconSize.MENU,
        )
        buttons["shutdown"].set_visible(vm.is_stoppable())

    def _paint_backdrop(self, widget, cr, width, height):
        """
        Paint the main panel background: the theme's window color with a
        soft light blue glow and a few layered curves on top. Everything
        is translucent, so it sits on light and dark themes alike
        """
        context = widget.get_style_context()
        base = context.lookup_color("theme_base_color")[1]
        bg = context.lookup_color("theme_bg_color")[1]
        mix = 0.45
        cr.set_source_rgb(
            base.red + (bg.red - base.red) * mix,
            base.green + (bg.green - base.green) * mix,
            base.blue + (bg.blue - base.blue) * mix,
        )
        cr.paint()

        blue = (0.33, 0.58, 0.98)
        sky = (0.36, 0.78, 0.98)
        span = max(width, height)

        # Glow from the top right corner, and a fainter one bottom left
        for cx, cy, radius, color, alpha in [
            (width, 0, span * 0.75, blue, 0.20),
            (0, height, span * 0.60, sky, 0.14),
        ]:
            glow = cairo.RadialGradient(cx, cy, 0, cx, cy, radius)
            glow.add_color_stop_rgba(0, *color, alpha)
            glow.add_color_stop_rgba(1, *color, 0)
            cr.set_source(glow)
            cr.paint()

        # Layered curves sweeping across the lower part of the panel
        for start, lift, color, alpha in [
            (0.52, 0.16, sky, 0.07),
            (0.66, 0.13, blue, 0.07),
            (0.80, 0.10, sky, 0.08),
        ]:
            y0 = height * start
            amp = height * lift
            cr.move_to(0, y0 + amp * 0.4)
            cr.curve_to(width * 0.30, y0 - amp, width * 0.62, y0 + amp, width, y0 - amp * 0.6)
            cr.line_to(width, height)
            cr.line_to(0, height)
            cr.close_path()
            cr.set_source_rgba(*color, alpha)
            cr.fill()

        # And one sweeping down from the top edge
        cr.move_to(width * 0.35, 0)
        cr.curve_to(width * 0.55, height * 0.20, width * 0.80, height * 0.02, width, height * 0.22)
        cr.line_to(width, 0)
        cr.close_path()
        cr.set_source_rgba(*blue, 0.07)
        cr.fill()

    def _backdrop_draw_cb(self, widget, cr):
        width = widget.get_allocated_width()
        height = widget.get_allocated_height()
        cr.save()
        cr.rectangle(0, 0, width, height)
        cr.clip()
        self._paint_backdrop(widget, cr, width, height)
        cr.restore()
        return False

    def _card_draw_cb(self, card, cr, backdrop):
        """
        Round the corners of the machine table. GTK3 can't clip children
        to a rounded border, so paint over the corners with the color
        behind the card, then draw the rounded outline on top
        """
        CARD_RADIUS = 10
        width = card.get_allocated_width()
        height = card.get_allocated_height()

        def rounded_path():
            rad = CARD_RADIUS
            right = width - rad - 0.5
            bottom = height - rad - 0.5
            cr.new_sub_path()
            cr.arc(right, rad + 0.5, rad, -math.pi / 2, 0)
            cr.arc(right, bottom, rad, 0, math.pi / 2)
            cr.arc(rad + 0.5, bottom, rad, math.pi / 2, math.pi)
            cr.arc(rad + 0.5, rad + 0.5, rad, math.pi, 3 * math.pi / 2)
            cr.close_path()

        context = backdrop.get_style_context()
        fg = context.get_color(context.get_state())

        # Repaint the backdrop over the four corner slivers
        cr.save()
        cr.set_fill_rule(cairo.FillRule.EVEN_ODD)
        cr.rectangle(0, 0, width, height)
        rounded_path()
        cr.clip()
        offset = card.translate_coordinates(backdrop, 0, 0)
        cr.translate(-offset[0], -offset[1])
        self._paint_backdrop(
            backdrop, cr, backdrop.get_allocated_width(), backdrop.get_allocated_height()
        )
        cr.restore()

        rounded_path()
        cr.set_line_width(1)
        cr.set_source_rgba(fg.red, fg.green, fg.blue, 0.14)
        cr.stroke()
        return False

    def init_stats(self):
        self.add_gsettings_handle(
            self.config.on_vmlist_guest_cpu_usage_visible_changed(
                self.toggle_guest_cpu_usage_visible_widget
            )
        )
        self.add_gsettings_handle(
            self.config.on_vmlist_host_cpu_usage_visible_changed(
                self.toggle_host_cpu_usage_visible_widget
            )
        )
        self.add_gsettings_handle(
            self.config.on_vmlist_memory_usage_visible_changed(
                self.toggle_memory_usage_visible_widget
            )
        )
        self.add_gsettings_handle(
            self.config.on_vmlist_disk_io_visible_changed(self.toggle_disk_io_visible_widget)
        )
        self.add_gsettings_handle(
            self.config.on_vmlist_network_traffic_visible_changed(
                self.toggle_network_traffic_visible_widget
            )
        )

        # Register callbacks with the global stats enable/disable values
        # that disable the associated vmlist widgets if reporting is disabled
        self.add_gsettings_handle(
            self.config.on_stats_enable_cpu_poll_changed(
                self._config_polling_change_cb, COL_GUEST_CPU
            )
        )
        self.add_gsettings_handle(
            self.config.on_stats_enable_disk_poll_changed(self._config_polling_change_cb, COL_DISK)
        )
        self.add_gsettings_handle(
            self.config.on_stats_enable_net_poll_changed(
                self._config_polling_change_cb, COL_NETWORK
            )
        )
        self.add_gsettings_handle(
            self.config.on_stats_enable_memory_poll_changed(self._config_polling_change_cb, COL_MEM)
        )

        self.toggle_guest_cpu_usage_visible_widget()
        self.toggle_host_cpu_usage_visible_widget()
        self.toggle_memory_usage_visible_widget()
        self.toggle_disk_io_visible_widget()
        self.toggle_network_traffic_visible_widget()

    def init_toolbar(self):
        self.widget("vm-new").set_icon_name("vm_new")
        self.widget("vm-open").set_icon_name("video-display-symbolic")
        self.widget("vm-run").set_icon_name("media-playback-start-symbolic")
        self.widget("vm-pause").set_icon_name("media-playback-pause-symbolic")

        self.widget("vm-shutdown").set_icon_name("system-shutdown-symbolic")
        for widgetname, classname in [
            ("vm-open", "vmm-act-open"),
            ("vm-run", "vmm-act-run"),
            ("vm-pause", "vmm-act-pause"),
            ("vm-shutdown", "vmm-act-stop"),
        ]:
            self.widget(widgetname).get_style_context().add_class(classname)
        self.widget("vm-shutdown").set_menu(self.shutdownmenu)

        tool = self.widget("vm-toolbar")
        tool.set_property("icon-size", Gtk.IconSize.SMALL_TOOLBAR)
        for c in tool.get_children():
            c.set_homogeneous(False)

    def init_context_menus(self):
        def add_to_menu(idx, text, cb):
            item = Gtk.MenuItem.new_with_mnemonic(text)
            if cb:
                item.connect("activate", cb)
            item.get_accessible().set_name("conn-%s" % idx)
            self.connmenu.add(item)
            self.connmenu_items[idx] = item

        # Build connection context menu
        add_to_menu("create", _("_New"), self.new_vm)
        add_to_menu("newgroup", _("New _Group..."), self.new_group)
        add_to_menu("connect", _("_Connect"), self.open_conn)
        add_to_menu("disconnect", _("Dis_connect"), self.close_conn)
        self.connmenu.add(Gtk.SeparatorMenuItem())
        add_to_menu("delete", _("De_lete"), self.do_delete_conn)
        self.connmenu.add(Gtk.SeparatorMenuItem())
        add_to_menu("details", _("_Details"), self.show_host)
        self.connmenu.show_all()

        # Add the 'Group' submenu to the VM context menu
        self.vmmenu.add(Gtk.SeparatorMenuItem())
        groupitem = Gtk.MenuItem.new_with_mnemonic(_("_Group"))
        groupitem.vmm_widget_name = "group"
        groupitem.set_submenu(_VMGroupMenu(self))
        self.vmmenu.add(groupitem)
        self.vmmenu.show_all()

        # Build group context menu
        def add_to_groupmenu(idx, text, cb):
            item = Gtk.MenuItem.new_with_mnemonic(text)
            item.connect("activate", cb)
            item.get_accessible().set_name("group-%s" % idx)
            self.groupmenu.add(item)
            self.groupmenu_items[idx] = item

        add_to_groupmenu("run", _("_Run All"), self.group_run_all)
        add_to_groupmenu("shutdown", _("_Shut Down All"), self.group_shutdown_all)
        self.groupmenu.add(Gtk.SeparatorMenuItem())
        add_to_groupmenu("rename", _("Re_name..."), self.group_rename)
        add_to_groupmenu("ungroup", _("_Delete Group"), self.group_delete)
        self.groupmenu.show_all()

    def init_vmlist(self):
        vmlist = self.widget("vm-list")
        vmlist.get_style_context().add_class("vmm-vmlist")

        rowtypes = []
        rowtypes.insert(ROW_HANDLE, object)  # backing VM object
        rowtypes.insert(ROW_SORT_KEY, str)  # VM name
        rowtypes.insert(ROW_MARKUP, str)  # name + group markup
        rowtypes.insert(ROW_HINT, str)  # row tooltip
        rowtypes.insert(ROW_STATUS_TEXT, str)  # status pill text
        rowtypes.insert(ROW_STATUS_COLOR, str)  # status pill color
        rowtypes.insert(ROW_IS_VM, bool)  # False for group heading rows
        rowtypes.insert(ROW_GROUP, str)  # group the row is listed under
        rowtypes.insert(ROW_BG, str)  # row background tint
        rowtypes.insert(ROW_INDENT, bool)  # VM row listed under a heading
        rowtypes.insert(ROW_GROUP_TEXT, str)  # text of the Group column

        model = Gtk.ListStore(*rowtypes)
        vmlist.set_model(model)
        vmlist.set_tooltip_column(ROW_HINT)
        vmlist.set_headers_visible(True)
        vmlist.set_grid_lines(Gtk.TreeViewGridLines.HORIZONTAL)

        # Group heading rows can't be selected
        def _select_cb(_selection, _model, path, _current):
            return _model[path][ROW_IS_VM]

        vmlist.get_selection().set_select_function(_select_cb)
        vmlist.connect("button-release-event", self._vm_list_button_release_cb)

        nameCol = Gtk.TreeViewColumn(_("Machine name"))
        nameCol.set_expand(True)
        nameCol.set_min_width(240)
        nameCol.set_sizing(Gtk.TreeViewColumnSizing.AUTOSIZE)
        nameCol.set_spacing(4)
        nameCol.set_sort_column_id(COL_NAME)
        vmlist.append_column(nameCol)

        # Indent for VM rows listed under a group heading
        indent = Gtk.CellRendererText()
        indent.set_fixed_size(GROUP_ROW_INDENT, -1)
        nameCol.pack_start(indent, False)
        nameCol.add_attribute(indent, "visible", ROW_INDENT)
        nameCol.add_attribute(indent, "cell-background", ROW_BG)

        tile = CellRendererIconTile()
        tile.set_property("icon-name", "computer-symbolic")
        tile.set_property("xpad", 8)
        tile.set_property("ypad", 5)
        nameCol.pack_start(tile, False)
        nameCol.add_attribute(tile, "visible", ROW_IS_VM)
        nameCol.add_attribute(tile, "cell-background", ROW_BG)

        name_txt = Gtk.CellRendererText()
        name_txt.set_property("ellipsize", Pango.EllipsizeMode.END)
        nameCol.pack_start(name_txt, True)
        name_txt.set_property("ypad", 4)
        name_txt.set_property("xpad", 8)
        nameCol.add_attribute(name_txt, "markup", ROW_MARKUP)
        nameCol.add_attribute(name_txt, "cell-background", ROW_BG)

        # Unused filler, kept so the stats toggling code can treat all
        # columns alike
        self.spacer_txt = Gtk.CellRendererText()
        self.spacer_txt.set_property("visible", False)
        nameCol.pack_end(self.spacer_txt, False)

        statusCol = Gtk.TreeViewColumn(_("Status"))
        statusCol.set_min_width(115)
        statusCol.set_sort_column_id(COL_STATUS)
        pill = CellRendererStatusPill()
        pill.set_property("xpad", 8)
        statusCol.pack_start(pill, False)
        statusCol.add_attribute(pill, "text", ROW_STATUS_TEXT)
        statusCol.add_attribute(pill, "color", ROW_STATUS_COLOR)
        statusCol.add_attribute(pill, "cell-background", ROW_BG)
        vmlist.append_column(statusCol)

        self._group_col = Gtk.TreeViewColumn(_("Group"))
        self._group_col.set_min_width(130)
        self._group_col.set_sort_column_id(COL_GROUP)
        group_txt = Gtk.CellRendererText()
        group_txt.set_property("xpad", 8)
        group_txt.set_property("ellipsize", Pango.EllipsizeMode.END)
        self._group_col.pack_start(group_txt, True)
        self._group_col.add_attribute(group_txt, "text", ROW_GROUP_TEXT)
        self._group_col.add_attribute(group_txt, "cell-background", ROW_BG)
        vmlist.append_column(self._group_col)

        def make_stats_column(title, colnum, textfunc=None):
            col = Gtk.TreeViewColumn(title)
            col.set_min_width(130)

            if textfunc:
                txt = Gtk.CellRendererText()
                txt.set_property("xpad", 8)
                txt.set_property("width-chars", 5)
                col.pack_start(txt, False)
                col.set_cell_data_func(txt, textfunc, None)
                col.add_attribute(txt, "cell-background", ROW_BG)
            else:
                # Empty filler: group heading rows hide the graph, and with
                # no visible cell the column would lose the heading's tint
                filler = Gtk.CellRendererText()
                col.pack_start(filler, False)
                col.add_attribute(filler, "cell-background", ROW_BG)

            img = CellRendererSparkline()
            img.set_property("xpad", 6)
            img.set_property("ypad", 9)
            img.set_property("reversed", True)
            img.set_property("xalign", 0)
            col.pack_start(img, True)
            col.add_attribute(img, "visible", ROW_IS_VM)
            col.add_attribute(img, "cell-background", ROW_BG)

            col.set_sort_column_id(colnum)
            vmlist.append_column(col)
            return col

        self.guestcpucol = make_stats_column(_("CPU"), COL_GUEST_CPU, self._guest_cpu_text_cb)
        self.hostcpucol = make_stats_column(_("Host CPU"), COL_HOST_CPU)

        memsizeCol = Gtk.TreeViewColumn(_("Memory"))
        memsizeCol.set_min_width(110)
        memsizeCol.set_sort_column_id(COL_MEMSIZE)
        membar = CellRendererMemBar()
        membar.set_property("xpad", 8)
        memsizeCol.pack_start(membar, False)
        memsizeCol.set_cell_data_func(membar, self._memsize_cb, None)
        memsizeCol.add_attribute(membar, "cell-background", ROW_BG)
        vmlist.append_column(memsizeCol)

        self.memcol = make_stats_column(_("Memory usage"), COL_MEM)
        self.diskcol = make_stats_column(_("Disk I/O"), COL_DISK)
        self.netcol = make_stats_column(_("Network I/O"), COL_NETWORK)

        for colnum, sorter in [
            (COL_NAME, self.vmlist_name_sorter),
            (COL_STATUS, self.vmlist_status_sorter),
            (COL_GROUP, self.vmlist_group_sorter),
            (COL_MEMSIZE, self.vmlist_memsize_sorter),
            (COL_GUEST_CPU, self.vmlist_guest_cpu_usage_sorter),
            (COL_HOST_CPU, self.vmlist_host_cpu_usage_sorter),
            (COL_MEM, self.vmlist_memory_usage_sorter),
            (COL_DISK, self.vmlist_disk_io_sorter),
            (COL_NETWORK, self.vmlist_network_usage_sorter),
        ]:
            model.set_sort_func(colnum, self._grouped_sorter, sorter)
        model.set_sort_column_id(COL_NAME, Gtk.SortType.ASCENDING)

    def _grouped_sorter(self, model, iter1, iter2, sorter):
        """
        Sort wrapper that keeps rows under their group heading: rows are
        ordered by group first, with the heading on top and the ungrouped
        machines last, and only then by the clicked column
        """

        def groupkey(row):
            return (not row[ROW_GROUP], (row[ROW_GROUP] or "").lower(), row[ROW_IS_VM])

        if self._grouping_active():
            ret = _cmp(groupkey(model[iter1]), groupkey(model[iter2]))
            if ret:
                # The view flips our answer for descending sorts, but the
                # group arrangement has to stay the same either way
                descending = model.get_sort_column_id()[1] == Gtk.SortType.DESCENDING
                return descending and -ret or ret
        return sorter(model, iter1, iter2, None)

    def _guest_cpu_text_cb(self, _col, cell, model, _iter, _data):
        vm = model[_iter][ROW_HANDLE]
        if vm is None:
            cell.set_property("text", "")
            return
        text = "—"
        if vm.is_active() and self.config.get_stats_enable_cpu_poll():
            text = "%d%%" % vm.guest_cpu_time_percentage()
        cell.set_property("text", text)

    def _memsize_cb(self, _col, cell, model, _iter, _data):
        vm = model[_iter][ROW_HANDLE]
        if vm is None:
            cell.set_property("text", "")
            cell.set_property("fraction", 0.0)
            return
        kib = _vm_memory_kib(vm)
        fraction = 0.0
        # Only show the usage bar when we have real polled usage numbers
        maxmem = vm.get_xmlobj().memory or 0
        if kib and maxmem and vm.stats_memory():
            fraction = min(1.0, float(kib) / maxmem)
        cell.set_property("text", kib and _pretty_memory(kib) or "—")
        cell.set_property("fraction", fraction)

    ##################
    # Helper methods #
    ##################

    @property
    def model(self):
        return self.widget("vm-list").get_model()

    def current_row(self):
        return uiutil.get_list_selected_row(self.widget("vm-list"))

    def current_vm(self):
        row = self.current_row()
        if not row:
            return None
        return row[ROW_HANDLE]

    def current_conn(self):
        return self._current_conn

    def current_group(self):
        """
        Name of the group selected in the sidebar, if any
        """
        if self._filter[0] == "group":
            return self._filter[1]
        return None

    def get_row(self, vm):
        for row in self.model:
            if row[ROW_IS_VM] and row[ROW_HANDLE] == vm:
                return row
        return None

    def _select_vm(self, vm):
        row = self.get_row(vm)
        if row is not None:
            self.widget("vm-list").get_selection().select_iter(row.iter)

    ####################
    # Action listeners #
    ####################

    def window_resized(self, ignore, ignore2):
        if not self.is_visible():
            return
        self._window_size = self.topwin.get_size()

    def exit_app(self, src_ignore=None, src2_ignore=None):
        vmmEngine.get_instance().exit_app()

    def open_newconn(self, _src):
        from .createconn import vmmCreateConn

        vmmCreateConn.get_instance(self).show(self.topwin)

    def new_vm(self, _src):
        from .createvm import vmmCreateVM

        conn = self.current_conn()
        vmmCreateVM.show_instance(self, conn and conn.get_uri() or None)

    def show_about(self, _src):
        from .about import vmmAbout

        vmmAbout.show_instance(self)

    def show_preferences(self, src_ignore):
        from .preferences import vmmPreferences

        vmmPreferences.show_instance(self)

    def show_host(self, _src):
        from .host import vmmHost

        conn = self.current_conn()
        if conn:
            vmmHost.show_instance(self, conn)

    def show_vm(self, _src):
        vmmenu.VMActionUI.show(self, self.current_vm())

    def row_activated(self, _src, path, *args):
        ignore = args
        row = self.model[path]
        if row[ROW_IS_VM]:
            # Use the activated row rather than the selection: the second
            # click of a double click may just have unselected it
            self._toggle_off_path = None
            self._select_vm(row[ROW_HANDLE])
            self.show_vm(_src)

    def do_delete(self, ignore=None):
        vm = self.current_vm()
        if vm:
            vmmenu.VMActionUI.delete(self, vm)

    def do_delete_conn(self, ignore=None):
        conn = self.current_conn()
        if conn is None:
            return  # pragma: no cover

        result = self.err.yes_no(
            _("This will remove the connection:\n\n%s\n\nAre you sure?") % conn.get_uri()
        )
        if not result:
            return

        vmmConnectionManager.get_instance().remove_conn(conn.get_uri())

    def set_pause_state(self, state):
        src = self.widget("vm-pause")
        try:
            src.handler_block_by_func(self.pause_vm_button)
            src.set_active(state)
        finally:
            src.handler_unblock_by_func(self.pause_vm_button)

    def pause_vm_button(self, src):
        do_pause = src.get_active()

        # Set button state back to original value: just let the status
        # update function fix things for us
        self.set_pause_state(not do_pause)

        if do_pause:
            vmmenu.VMActionUI.suspend(self, self.current_vm())
        else:
            vmmenu.VMActionUI.resume(self, self.current_vm())

    def start_vm(self, ignore):
        vmmenu.VMActionUI.run(self, self.current_vm())

    def poweroff_vm(self, _src):
        vmmenu.VMActionUI.shutdown(self, self.current_vm())

    def close_conn(self, ignore):
        conn = self.current_conn()
        if not conn.is_disconnected():
            conn.close()

    def open_conn(self, ignore=None):
        conn = self.current_conn()
        if conn.is_disconnected():
            conn.connect_once("open-completed", self._conn_open_completed_cb)
            conn.open()
            return True

    def _conn_open_completed_cb(self, _conn, ConnectError):
        if ConnectError:
            msg, details, title = ConnectError
            self.err.show_err(msg, details, title)

    ###########################
    # Sidebar: connection list #
    ###########################

    def _make_side_row(self, icon_name, title, subtitle_markup=None, count=None):
        row = Gtk.ListBoxRow()
        box = Gtk.Box(spacing=12)
        box.add(Gtk.Image.new_from_icon_name(icon_name, Gtk.IconSize.MENU))

        title_label = Gtk.Label(label=title, xalign=0)
        title_label.set_ellipsize(Pango.EllipsizeMode.END)
        if subtitle_markup:
            vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
            sub = Gtk.Label(xalign=0)
            sub.set_markup("<span size='smaller'>%s</span>" % subtitle_markup)
            sub.set_ellipsize(Pango.EllipsizeMode.END)
            vbox.add(title_label)
            vbox.add(sub)
            box.pack_start(vbox, True, True, 0)
        else:
            box.pack_start(title_label, True, True, 0)

        if count is not None:
            count_label = Gtk.Label(label=str(count))
            count_label.get_style_context().add_class("vmm-dim")
            box.pack_end(count_label, False, False, 0)

        row.add(box)
        return row

    def _conn_status_markup(self, conn):
        if conn.is_active():
            return _dot_markup(COLOR_RUNNING, _("Connected"))
        if conn.is_connecting():
            return _dot_markup(COLOR_PAUSED, _("Connecting..."))
        return _dot_markup(COLOR_OFF, _("Not connected"))

    def _refresh_conn_list(self):
        self._sidebar_updating = True
        try:
            for child in self._conn_list.get_children():
                self._conn_list.remove(child)
                child.destroy()

            for conn in self._conns:
                row = self._make_side_row(
                    "network-server-symbolic",
                    conn.get_pretty_desc(),
                    subtitle_markup=self._conn_status_markup(conn),
                )
                row.get_style_context().add_class("vmm-conn-row")
                row.set_tooltip_text(self._build_conn_hint(conn))
                row.get_accessible().set_name(self._build_conn_a11y_name(conn))
                row.vmm_conn = conn
                self._conn_list.add(row)
                if conn == self._current_conn:
                    self._conn_list.select_row(row)
            self._conn_list.show_all()
        finally:
            self._sidebar_updating = False

    def _build_conn_a11y_name(self, conn):
        name = conn.get_pretty_desc()
        if conn.is_disconnected():
            return _("%(connection)s - Not Connected") % {"connection": name}
        if conn.is_connecting():
            return _("%(connection)s - Connecting...") % {"connection": name}
        return name

    def _build_conn_hint(self, conn):
        hint = conn.get_uri()
        if conn.is_disconnected():
            hint = _("%(uri)s (Double click to connect)") % {"uri": conn.get_uri()}
        return hint

    def _set_current_conn(self, conn):
        if conn == self._current_conn:
            return
        self._current_conn = conn
        self._filter = FILTER_ALL
        self._refresh_all()

    def _conn_row_selected_cb(self, _src, row):
        if self._sidebar_updating or row is None:
            return
        # From idle, see _filter_row_selected_cb
        self.idle_add(self._set_current_conn, row.vmm_conn)

    def _conn_row_activated_cb(self, _src, row):
        self._set_current_conn(row.vmm_conn)
        conn = row.vmm_conn
        if conn.is_disconnected():
            self.open_conn()
        else:
            self.show_host(_src)

    def _conn_list_button_press_cb(self, listbox, event):
        if event.button != 3:
            return False
        row = listbox.get_row_at_y(int(event.y))
        if row is None:
            return False  # pragma: no cover

        listbox.select_row(row)
        conn = row.vmm_conn
        disconn = conn.is_disconnected()
        conning = conn.is_connecting()

        self.connmenu_items["create"].set_sensitive(not disconn)
        self.connmenu_items["newgroup"].set_sensitive(conn.is_active())
        self.connmenu_items["disconnect"].set_sensitive(not (disconn or conning))
        self.connmenu_items["connect"].set_sensitive(disconn)
        self.connmenu_items["delete"].set_sensitive(disconn)
        self.connmenu.popup_at_pointer(event)
        return True

    def set_initial_selection(self, uri):
        """
        Select the passed URI in the UI. Called from engine.py via
        cli --connect $URI
        """
        for conn in self._conns:
            if conn.get_uri() == uri:
                self._set_current_conn(conn)
                self._refresh_conn_list()
                return

    #########################################
    # Sidebar: workspace filters and groups #
    #########################################

    def _conn_vms(self, conn=None):
        conn = conn or self._current_conn
        if conn is None or not conn.is_active():
            return []
        return conn.list_vms()

    def _group_vms(self, conn, groupname):
        return [vm for vm in self._conn_vms(conn) if vm.get_group() == groupname]

    def _filtered_vms(self):
        vms = self._conn_vms()
        kind, groupname = self._filter
        if kind == "ungrouped":
            return [vm for vm in vms if not vm.get_group()]
        if kind == "group":
            return [vm for vm in vms if vm.get_group() == groupname]
        return vms

    def list_group_names(self, conn):
        """
        Groups of the connection: the saved ones, which may be empty,
        plus any that VMs are assigned to
        """
        if conn is None or not conn.is_active():
            return []
        names = set(conn.get_vm_groups())
        names.update(vm.get_group() for vm in conn.list_vms() if vm.get_group())
        return sorted(names, key=str.lower)

    def _refresh_filter_list(self):
        conn = self._current_conn
        vms = self._conn_vms()
        groupnames = self.list_group_names(conn)
        if self._filter[0] == "group" and self._filter[1] not in groupnames:
            self._filter = FILTER_ALL

        self._sidebar_updating = True
        try:
            for child in self._filter_list.get_children():
                self._filter_list.remove(child)
                child.destroy()

            def add_row(filterval, icon_name, title, count):
                row = self._make_side_row(icon_name, title, count=count)
                row.vmm_filter = filterval
                row.get_accessible().set_name("%s (%d)" % (title, count))
                self._filter_list.add(row)
                if filterval == self._filter:
                    self._filter_list.select_row(row)

            ungrouped = [vm for vm in vms if not vm.get_group()]
            add_row(FILTER_ALL, "view-grid-symbolic", _("All machines"), len(vms))
            add_row(FILTER_UNGROUPED, "computer-symbolic", _("Ungrouped"), len(ungrouped))
            for name in groupnames:
                count = len([vm for vm in vms if vm.get_group() == name])
                add_row(_filter_group(name), "folder-symbolic", name, count)
            self._filter_list.show_all()
        finally:
            self._sidebar_updating = False

        self._new_group_button.set_sensitive(bool(conn and conn.is_active()))

    def _scroll_vmlist_to_top(self):
        def _scroll():
            vmlist = self.widget("vm-list")
            if vmlist.get_realized():
                vmlist.scroll_to_point(0, 0)

        # From idle, once the list has laid out its new rows
        self.idle_add(_scroll)

    def _set_filter(self, filterval):
        if filterval == self._filter:
            return
        self._filter = filterval
        self._refresh_all()
        self._scroll_vmlist_to_top()

    def _filter_row_selected_cb(self, _src, row):
        if self._sidebar_updating or row is None:
            return
        # From idle: switching rebuilds the rows, which must not happen
        # while the list box is still handling the click on one of them
        self.idle_add(self._set_filter, row.vmm_filter)

    def _filter_list_button_press_cb(self, listbox, event):
        if event.button != 3:
            return False
        row = listbox.get_row_at_y(int(event.y))
        if row is None or row.vmm_filter[0] != "group":
            return False

        listbox.select_row(row)
        vms = self._group_vms(self._current_conn, row.vmm_filter[1])
        self.groupmenu_items["run"].set_sensitive(any(vm.is_runable() for vm in vms))
        self.groupmenu_items["shutdown"].set_sensitive(any(vm.is_stoppable() for vm in vms))
        self.groupmenu.popup_at_pointer(event)
        return True

    #########################
    # VM grouping actions #
    #########################

    def _save_group(self, conn, groupname):
        groups = conn.get_vm_groups()
        if groupname not in groups:
            conn.set_vm_groups(groups + [groupname])

    def _unsave_group(self, conn, groupname):
        groups = conn.get_vm_groups()
        if groupname in groups:
            groups.remove(groupname)
            conn.set_vm_groups(groups)

    def set_vm_group(self, vm, groupname):
        """
        Move the VM to the passed group, creating the group if needed.
        An empty groupname makes the VM ungrouped
        """
        groupname = (groupname or "").strip()
        if groupname == vm.get_group():
            return

        if groupname:
            self._save_group(vm.conn, groupname)
        vm.set_group(groupname)
        self._refresh_all()

    def _ask_group_name(self, title, text, default=""):
        dialog = Gtk.Dialog(title=title, transient_for=self.topwin, modal=True)
        dialog.add_button(_("_Cancel"), Gtk.ResponseType.CANCEL)
        dialog.add_button(_("_OK"), Gtk.ResponseType.OK)
        dialog.set_default_response(Gtk.ResponseType.OK)

        box = dialog.get_content_area()
        box.set_border_width(12)
        box.set_spacing(6)
        label = Gtk.Label(label=text, xalign=0)
        entry = Gtk.Entry()
        entry.set_text(default)
        entry.set_activates_default(True)
        entry.get_accessible().set_name("group-name-entry")
        box.add(label)
        box.add(entry)
        box.show_all()

        response = dialog.run()
        name = entry.get_text().strip()
        dialog.destroy()
        if response != Gtk.ResponseType.OK:
            return None
        return name or None

    def set_vm_new_group(self, vm):
        name = self._ask_group_name(
            _("New Group"), _("Name of the new group for '%s':") % vm.get_name_or_title()
        )
        if name:
            self.set_vm_group(vm, name)

    def new_group(self, _src):
        conn = self.current_conn()
        if conn is None or not conn.is_active():
            return  # pragma: no cover

        name = self._ask_group_name(_("New Group"), _("Name of the new group:"))
        if not name:
            return

        self._save_group(conn, name)
        self._filter = _filter_group(name)
        self._refresh_all()

    def group_rename(self, _src):
        conn = self.current_conn()
        oldname = self.current_group()
        if not oldname:
            return  # pragma: no cover

        name = self._ask_group_name(_("Rename Group"), _("New name for the group:"), oldname)
        if not name or name == oldname:
            return

        # If the name matches an existing group, this merges the two
        vms = self._group_vms(conn, oldname)
        self._unsave_group(conn, oldname)
        self._save_group(conn, name)
        for vm in vms:
            vm.set_group(name)
        self._filter = _filter_group(name)
        self._refresh_all()

    def group_delete(self, _src):
        """
        Remove the group. Its VMs are kept and become ungrouped
        """
        conn = self.current_conn()
        groupname = self.current_group()
        if not groupname:
            return  # pragma: no cover

        vms = self._group_vms(conn, groupname)
        self._unsave_group(conn, groupname)
        for vm in vms:
            vm.set_group("")
        self._filter = FILTER_ALL
        self._refresh_all()

    def group_run_all(self, _src):
        groupname = self.current_group()
        if not groupname:
            return  # pragma: no cover
        for vm in self._group_vms(self.current_conn(), groupname):
            if vm.is_runable():
                vmmenu.VMActionUI.run(self, vm)

    def group_shutdown_all(self, _src):
        groupname = self.current_group()
        if not groupname:
            return  # pragma: no cover

        vms = [vm for vm in self._group_vms(self.current_conn(), groupname) if vm.is_stoppable()]
        if not vms:
            return  # pragma: no cover
        if not self.err.yes_no(
            _("Are you sure you want to poweroff all running VMs in group '%s'?") % groupname,
            "\n".join(vm.get_name() for vm in vms),
        ):
            return

        for vm in vms:
            log.debug("Shutting down vm '%s'", vm.get_name())
            vmmAsyncJob.simple_async_noshow(vm.shutdown, [], self, _("Error shutting down domain"))

    #####################################
    # VM/conn add/remove and row updates #
    #####################################

    def _build_vm_markup(self, vm):
        return xmlutil.xml_escape(vm.get_name_or_title())

    def _set_vm_row(self, row, vm):
        row[ROW_SORT_KEY] = vm.get_name_or_title()
        row[ROW_MARKUP] = self._build_vm_markup(vm)
        row[ROW_HINT] = xmlutil.xml_escape(vm.get_description())
        row[ROW_STATUS_TEXT] = vm.run_status()
        row[ROW_STATUS_COLOR] = _vm_status_color(vm)
        row[ROW_IS_VM] = True
        row[ROW_GROUP] = vm.get_group()
        row[ROW_BG] = None
        row[ROW_INDENT] = self._grouping_active()
        row[ROW_GROUP_TEXT] = vm.get_group() or _("Ungrouped")

    def _grouping_active(self):
        """
        If the list is arranged under group headings, which is always
        the case for the 'All machines' view
        """
        return self._filter == FILTER_ALL

    def _collapse_key(self, groupname):
        return (self._current_conn and self._current_conn.get_uri(), groupname or "")

    def _is_group_collapsed(self, groupname):
        return self._collapse_key(groupname) in self._collapsed_groups

    def _toggle_group_collapsed(self, groupname):
        key = self._collapse_key(groupname)
        if key in self._collapsed_groups:
            self._collapsed_groups.remove(key)
        else:
            self._collapsed_groups.add(key)
        self._refresh_vm_rows()
        self.update_current_selection()

    def _refresh_heading_rows(self):
        """
        Make the group heading rows match the grouped 'All machines' view.
        Rows are updated in place where possible: removing and re-adding
        the row at the top of the list makes the view scroll away from it
        """
        headings = {row[ROW_GROUP]: row for row in self.model if not row[ROW_IS_VM]}

        groupnames = []
        vms = []
        if self._grouping_active():
            # Count from all the machines, not the listed rows: the rows
            # of a collapsed group are not in the list
            vms = self._filtered_vms()
            groupnames = self.list_group_names(self._current_conn)
            if any(not vm.get_group() for vm in vms):
                groupnames.append("")

        for groupname, row in headings.items():
            if groupname not in groupnames:
                self.model.remove(row.iter)

        hint = _("Click to collapse or expand this group")
        for groupname in groupnames:
            members = [vm for vm in vms if vm.get_group() == groupname]
            nrunning = len([vm for vm in members if vm.is_active()])
            count = ngettext("%d machine", "%d machines", len(members)) % len(members)
            if members:
                count += " \u00b7 " + _("%d running") % nrunning
            arrow = self._is_group_collapsed(groupname) and "\u25b8" or "\u25be"
            markup = "%s  <b>%s</b>   <span size='smaller' alpha='60%%'>%s</span>" % (
                arrow,
                xmlutil.xml_escape(groupname or _("Ungrouped")),
                xmlutil.xml_escape(count),
            )

            if groupname in headings:
                if headings[groupname][ROW_MARKUP] != markup:
                    headings[groupname][ROW_MARKUP] = markup
                continue
            self.model.append(
                [
                    None,
                    groupname,
                    markup,
                    hint,
                    None,
                    None,
                    False,
                    groupname,
                    BG_GROUP_HEADING,
                    False,
                    "",
                ]
            )

    def _refresh_vm_rows(self):
        """
        Make the list match the VMs selected by the sidebar, keeping
        the current selection if that VM is still listed
        """
        selected = self.current_vm()
        wanted = self._filtered_vms()
        grouping = self._grouping_active()
        if grouping:
            wanted = [vm for vm in wanted if not self._is_group_collapsed(vm.get_group())]

        for row in list(self.model):
            if row[ROW_IS_VM] and row[ROW_HANDLE] not in wanted:
                self.model.remove(row.iter)
        listed = [row[ROW_HANDLE] for row in self.model if row[ROW_IS_VM]]
        for vm in wanted:
            if vm in listed:
                self._set_vm_row(self.get_row(vm), vm)
            else:
                rowiter = self.model.append(
                    [vm, "", "", None, "", None, True, "", None, False, ""]
                )
                self._set_vm_row(self.model[rowiter], vm)
        self._refresh_heading_rows()

        # Switching between the grouped and the flat views changes how rows
        # compare without changing any row, so make the model sort again
        colnum, order = self.model.get_sort_column_id()
        if colnum is not None:
            self.model.set_sort_column_id(Gtk.TREE_SORTABLE_UNSORTED_SORT_COLUMN_ID, order)
            self.model.set_sort_column_id(colnum, order)

        if selected in wanted:
            self._select_vm(selected)

    def _refresh_header(self):
        conn = self._current_conn
        title = _("Virtual machines")
        if self._filter == FILTER_UNGROUPED:
            title = _("Ungrouped")
        elif self.current_group():
            title = self.current_group()
        self._title_label.set_text(title)

        if conn is None:
            summary = xmlutil.xml_escape(_("No connection"))
        elif not conn.is_active():
            summary = self._conn_status_markup(conn)
        else:
            vms = self._filtered_vms()
            nrunning = len([vm for vm in vms if vm.is_active()])
            parts = [
                xmlutil.xml_escape(
                    ngettext("%d machine", "%d machines", len(vms)) % len(vms)
                ),
                _dot_markup(COLOR_RUNNING, _("%d running") % nrunning),
                _dot_markup(COLOR_OFF, _("%d stopped") % (len(vms) - nrunning)),
            ]
            summary = "      ".join(parts)
        self._summary_label.set_markup(summary)

    def _refresh_all(self):
        self._refresh_filter_list()
        self._refresh_vm_rows()
        self._refresh_header()
        self.update_current_selection()

    def vm_added(self, conn, vm):
        vm.connect("state-changed", self.vm_changed)
        vm.connect("resources-sampled", self.vm_row_updated)
        if conn == self._current_conn:
            self._refresh_all()

    def vm_removed(self, conn, vm):
        ignore = vm
        if conn == self._current_conn:
            self._refresh_all()

    def _conn_added(self, _src, conn):
        # Make sure error page isn't showing
        self.widget("vm-notebook").set_current_page(0)
        if conn in self._conns:
            return  # pragma: no cover

        self._conns.append(conn)
        conn.connect("vm-added", self.vm_added)
        conn.connect("vm-removed", self.vm_removed)
        conn.connect("resources-sampled", self.conn_row_updated)
        conn.connect("state-changed", self.conn_state_changed)

        for vm in conn.list_vms():
            vm.connect("state-changed", self.vm_changed)
            vm.connect("resources-sampled", self.vm_row_updated)

        if self._current_conn is None:
            self._current_conn = conn
        self._refresh_conn_list()
        self._refresh_all()

    def _conn_removed(self, _src, uri):
        conn = next((c for c in self._conns if c.get_uri() == uri), None)
        if conn is None:  # pragma: no cover
            return

        self._conns.remove(conn)
        if conn == self._current_conn:
            self._current_conn = self._conns and self._conns[0] or None
            self._filter = FILTER_ALL
        self._refresh_conn_list()
        self._refresh_all()

    #############################
    # State/UI updating methods #
    #############################

    def vm_row_updated(self, vm):
        row = self.get_row(vm)
        if row is None:
            return
        self.model.row_changed(row.path, row.iter)

    def vm_changed(self, vm):
        if vm.conn != self._current_conn:
            return

        try:
            row = self.get_row(vm)
            if row is not None:
                self._set_vm_row(row, vm)
                self._refresh_heading_rows()
            self._refresh_header()
            if vm == self.current_vm():
                self.update_current_selection()
        except Exception as e:  # pragma: no cover
            if vm.conn.support.is_libvirt_error_no_domain(e):
                return
            raise

    def conn_state_changed(self, conn):
        self._refresh_conn_list()
        if conn == self._current_conn:
            self._refresh_all()

    def conn_row_updated(self, conn):
        self.max_disk_rate = max(self.max_disk_rate, conn.disk_io_max_rate())
        self.max_net_rate = max(self.max_net_rate, conn.network_traffic_max_rate())

    def change_run_text(self, can_restore):
        if can_restore:
            text = _("_Restore")
        else:
            text = _("_Run")
        strip_text = text.replace("_", "")

        self.vmmenu.change_run_text(text)
        self.widget("vm-run").set_label(strip_text)

    def update_current_selection(self, ignore=None):
        vm = self.current_vm()
        conn = self.current_conn()
        conn_active = bool(conn and conn.is_active())

        show_open = bool(vm)
        show_details = bool(vm)
        host_details = bool(conn)
        can_delete = bool(vm)

        show_run = bool(vm and vm.is_runable())
        is_paused = bool(vm and vm.is_paused())
        if is_paused:
            show_pause = bool(vm and vm.is_unpauseable())
        else:
            show_pause = bool(vm and vm.is_pauseable())
        show_shutdown = bool(vm and vm.is_stoppable())

        if vm and vm.managedsave_supported:
            self.change_run_text(vm.has_managed_save())

        self.widget("vm-open").set_sensitive(show_open)
        self.widget("vm-run").set_sensitive(show_run)
        self.widget("vm-shutdown").set_sensitive(show_shutdown)
        self.widget("vm-shutdown").get_menu().update_widget_states(vm)

        self.set_pause_state(is_paused)
        self.widget("vm-pause").set_sensitive(show_pause)

        if is_paused:
            pauseTooltip = _("Resume the virtual machine")
        else:
            pauseTooltip = _("Pause the virtual machine")
        self.widget("vm-pause").set_tooltip_text(pauseTooltip)

        self.widget("menu_edit_delete").set_sensitive(can_delete)
        self.widget("menu_edit_details").set_sensitive(show_details)
        self.widget("menu_host_details").set_sensitive(host_details)
        self.widget("menu-new-group").set_sensitive(conn_active)
        self._create_button.set_sensitive(conn_active)
        self._refresh_detail_card(vm)

    def _unselect_vm(self):
        """
        Drop the VM selection, which also closes the detail card
        """
        self.widget("vm-list").get_selection().unselect_all()

    def _window_button_press_cb(self, _src, event):
        # Clicks that no widget handled land here: on the page background,
        # headings and so on. Treat those as clicking away from the VM,
        # unless they belong to the detail card, VM list, or toolbar
        widget = Gtk.get_event_widget(event)
        while widget is not None:
            if widget in (
                self._detail_revealer,
                self.widget("vm-list"),
                self.widget("vm-toolbar"),
            ):
                return False
            widget = widget.get_parent()
        self._unselect_vm()
        return False

    def popup_vm_menu_key(self, widget_ignore, event):
        if Gdk.keyval_name(event.keyval) == "Escape":
            self._unselect_vm()
            return True
        if Gdk.keyval_name(event.keyval) != "Menu":
            return False  # pragma: no cover

        model, treeiter = self.widget("vm-list").get_selection().get_selected()
        if treeiter is None:
            return False  # pragma: no cover
        self.popup_vm_menu(model, treeiter, event)
        return True

    def popup_vm_menu_button(self, vmlist, event):
        if event.button not in [1, 3]:
            return False

        tup = vmlist.get_path_at_pos(int(event.x), int(event.y))
        if tup is None:
            # Click on the empty part of the table
            self._unselect_vm()
            return False
        path = tup[0]
        if not self.model[path][ROW_IS_VM]:
            # Group heading: a click collapses or expands the group
            if event.button == 1 and event.type == Gdk.EventType.BUTTON_PRESS:
                self._toggle_group_collapsed(self.model[path][ROW_GROUP])
            return True
        if event.button == 1:
            # A plain click on the already selected VM unselects it again,
            # which closes the detail card. That happens on release, see
            # _vm_list_button_release_cb: the press has to reach the list
            # untouched for double click detection to keep working
            selection = vmlist.get_selection()
            self._toggle_off_path = None
            if event.type == Gdk.EventType.BUTTON_PRESS and selection.path_is_selected(path):
                self._toggle_off_path = path
            return False

        vmlist.get_selection().select_path(path)
        self.popup_vm_menu(self.model, self.model.get_iter(path), event)
        return True

    def _vm_list_button_release_cb(self, vmlist, event):
        path = self._toggle_off_path
        self._toggle_off_path = None
        if event.button != 1 or path is None:
            return False

        tup = vmlist.get_path_at_pos(int(event.x), int(event.y))
        if tup is not None and tup[0] == path:
            vmlist.get_selection().unselect_path(path)
        return False

    def popup_vm_menu(self, model, _iter, event):
        vm = model[_iter][ROW_HANDLE]
        if vm is None:
            return
        self.vmmenu.update_widget_states(vm)
        self.vmmenu.popup_at_pointer(event)

    #################
    # Stats methods #
    #################

    def vmlist_name_sorter(self, model, iter1, iter2, ignore):
        key1 = str(model[iter1][ROW_SORT_KEY]).lower()
        key2 = str(model[iter2][ROW_SORT_KEY]).lower()
        return _cmp(key1, key2)

    def vmlist_group_sorter(self, model, iter1, iter2, ignore):
        # Ungrouped machines go last
        def key(row):
            group = row[ROW_GROUP] or ""
            return (not group, group.lower(), str(row[ROW_SORT_KEY]).lower())

        return _cmp(key(model[iter1]), key(model[iter2]))

    def vmlist_status_sorter(self, model, iter1, iter2, ignore):
        key1 = (model[iter1][ROW_STATUS_TEXT], str(model[iter1][ROW_SORT_KEY]).lower())
        key2 = (model[iter2][ROW_STATUS_TEXT], str(model[iter2][ROW_SORT_KEY]).lower())
        return _cmp(key1, key2)

    def vmlist_memsize_sorter(self, model, iter1, iter2, ignore):
        mem1 = _vm_memory_kib(model[iter1][ROW_HANDLE])
        mem2 = _vm_memory_kib(model[iter2][ROW_HANDLE])
        return _cmp(mem1, mem2)

    def vmlist_guest_cpu_usage_sorter(self, model, iter1, iter2, ignore):
        obj1 = model[iter1][ROW_HANDLE]
        obj2 = model[iter2][ROW_HANDLE]

        return _cmp(obj1.guest_cpu_time_percentage(), obj2.guest_cpu_time_percentage())

    def vmlist_host_cpu_usage_sorter(self, model, iter1, iter2, ignore):
        obj1 = model[iter1][ROW_HANDLE]
        obj2 = model[iter2][ROW_HANDLE]

        return _cmp(obj1.host_cpu_time_percentage(), obj2.host_cpu_time_percentage())

    def vmlist_memory_usage_sorter(self, model, iter1, iter2, ignore):
        obj1 = model[iter1][ROW_HANDLE]
        obj2 = model[iter2][ROW_HANDLE]

        return _cmp(obj1.stats_memory(), obj2.stats_memory())

    def vmlist_disk_io_sorter(self, model, iter1, iter2, ignore):
        obj1 = model[iter1][ROW_HANDLE]
        obj2 = model[iter2][ROW_HANDLE]

        return _cmp(obj1.disk_io_rate(), obj2.disk_io_rate())

    def vmlist_network_usage_sorter(self, model, iter1, iter2, ignore):
        obj1 = model[iter1][ROW_HANDLE]
        obj2 = model[iter2][ROW_HANDLE]

        return _cmp(obj1.network_traffic_rate(), obj2.network_traffic_rate())

    def _config_polling_change_cb(self, column):
        # pylint: disable=redefined-variable-type
        if column == COL_GUEST_CPU:
            widgn = ["menu_view_stats_guest_cpu", "menu_view_stats_host_cpu"]
            do_enable = self.config.get_stats_enable_cpu_poll()
        if column == COL_DISK:
            widgn = "menu_view_stats_disk"
            do_enable = self.config.get_stats_enable_disk_poll()
        elif column == COL_NETWORK:
            widgn = "menu_view_stats_network"
            do_enable = self.config.get_stats_enable_net_poll()
        elif column == COL_MEM:
            widgn = "menu_view_stats_memory"
            do_enable = self.config.get_stats_enable_memory_poll()

        for w in xmlutil.listify(widgn):
            widget = self.widget(w)
            tool_text = ""

            if do_enable:
                widget.set_sensitive(True)
            else:
                widget.set_active(False)
                widget.set_sensitive(False)
                tool_text = _("Disabled in preferences dialog.")
            widget.set_tooltip_text(tool_text)

    def _toggle_graph_helper(self, do_show, col, datafunc, menu):
        img = -1
        for child in col.get_cells():
            if isinstance(child, CellRendererSparkline):
                img = child
        datafunc = do_show and datafunc or None

        col.set_cell_data_func(img, datafunc, None)
        col.set_visible(do_show)
        self.widget(menu).set_active(do_show)

        any_visible = any(
            [
                c.get_visible()
                for c in [self.netcol, self.diskcol, self.memcol, self.guestcpucol, self.hostcpucol]
            ]
        )
        self.spacer_txt.set_property("visible", not any_visible)

    def toggle_network_traffic_visible_widget(self):
        self._toggle_graph_helper(
            self.config.is_vmlist_network_traffic_visible(),
            self.netcol,
            self.network_traffic_img,
            "menu_view_stats_network",
        )

    def toggle_disk_io_visible_widget(self):
        self._toggle_graph_helper(
            self.config.is_vmlist_disk_io_visible(),
            self.diskcol,
            self.disk_io_img,
            "menu_view_stats_disk",
        )

    def toggle_memory_usage_visible_widget(self):
        self._toggle_graph_helper(
            self.config.is_vmlist_memory_usage_visible(),
            self.memcol,
            self.memory_usage_img,
            "menu_view_stats_memory",
        )

    def toggle_guest_cpu_usage_visible_widget(self):
        self._toggle_graph_helper(
            self.config.is_vmlist_guest_cpu_usage_visible(),
            self.guestcpucol,
            self.guest_cpu_usage_img,
            "menu_view_stats_guest_cpu",
        )

    def toggle_host_cpu_usage_visible_widget(self):
        self._toggle_graph_helper(
            self.config.is_vmlist_host_cpu_usage_visible(),
            self.hostcpucol,
            self.host_cpu_usage_img,
            "menu_view_stats_host_cpu",
        )

    def toggle_stats_visible(self, src, stats_id):
        visible = src.get_active()
        set_stats = {
            COL_GUEST_CPU: self.config.set_vmlist_guest_cpu_usage_visible,
            COL_HOST_CPU: self.config.set_vmlist_host_cpu_usage_visible,
            COL_MEM: self.config.set_vmlist_memory_usage_visible,
            COL_DISK: self.config.set_vmlist_disk_io_visible,
            COL_NETWORK: self.config.set_vmlist_network_traffic_visible,
        }
        set_stats[stats_id](visible)

    def toggle_stats_visible_guest_cpu(self, src):
        self.toggle_stats_visible(src, COL_GUEST_CPU)

    def toggle_stats_visible_host_cpu(self, src):
        self.toggle_stats_visible(src, COL_HOST_CPU)

    def toggle_stats_visible_memory_usage(self, src):
        self.toggle_stats_visible(src, COL_MEM)

    def toggle_stats_visible_disk(self, src):
        self.toggle_stats_visible(src, COL_DISK)

    def toggle_stats_visible_network(self, src):
        self.toggle_stats_visible(src, COL_NETWORK)

    def guest_cpu_usage_img(self, column_ignore, cell, model, _iter, data):
        obj = model[_iter][ROW_HANDLE]
        if obj is None or not hasattr(obj, "conn"):
            return

        data = obj.guest_cpu_time_vector(GRAPH_LEN)
        cell.set_property("data_array", data)
        cell.set_property("visible", obj.is_active())

    def host_cpu_usage_img(self, column_ignore, cell, model, _iter, data):
        obj = model[_iter][ROW_HANDLE]
        if obj is None or not hasattr(obj, "conn"):
            return

        data = obj.host_cpu_time_vector(GRAPH_LEN)
        cell.set_property("data_array", data)

    def memory_usage_img(self, column_ignore, cell, model, _iter, data):
        obj = model[_iter][ROW_HANDLE]
        if obj is None or not hasattr(obj, "conn"):
            return

        data = obj.stats_memory_vector(GRAPH_LEN)
        cell.set_property("data_array", data)

    def disk_io_img(self, column_ignore, cell, model, _iter, data):
        obj = model[_iter][ROW_HANDLE]
        if obj is None or not hasattr(obj, "conn"):
            return

        d1, d2 = obj.disk_io_vectors(GRAPH_LEN, self.max_disk_rate)
        data = [(x + y) / 2 for x, y in zip(d1, d2)]
        cell.set_property("data_array", data)

    def network_traffic_img(self, column_ignore, cell, model, _iter, data):
        obj = model[_iter][ROW_HANDLE]
        if obj is None or not hasattr(obj, "conn"):
            return

        d1, d2 = obj.network_traffic_vectors(GRAPH_LEN, self.max_net_rate)
        data = [(x + y) / 2 for x, y in zip(d1, d2)]
        cell.set_property("data_array", data)
