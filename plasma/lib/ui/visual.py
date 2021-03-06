#!/usr/bin/env python3
#
# PLASMA : Generate an indented asm code (pseudo-C) with colored syntax.
# Copyright (C) 2015    Joel
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.    See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.    If not, see <http://www.gnu.org/licenses/>.
#

import curses
import traceback

from plasma.lib.utils import error, die
from plasma.lib.custom_colors import *
from plasma.lib.ui.window import *
from plasma.lib.ui.inlineed import InlineEd
from plasma.lib.consts import *


class Visual(Window):
    def __init__(self, gctx, ctx, analyzer, api, stack, saved_stack):
        Window.__init__(self, ctx.output, has_statusbar=True)

        self.ctx = ctx
        self.gctx = gctx
        self.mode = MODE_DUMP
        self.dis = gctx.dis
        self.db = gctx.db
        self.analyzer = analyzer
        self.api = api

        # Last/first address printed (only in MODE_DUMP)
        self.set_last_addr()
        self.first_addr = min(self.output.addr_line)

        self.last_curr_line_ad = None

        self.stack = stack
        self.saved_stack = saved_stack # when we enter, go back, then re-enter

        new_mapping = {
            b"z": self.main_cmd_line_middle,
            b"g": self.main_k_top,
            b"G": self.main_k_bottom,
            b";": self.view_inline_comment_editor,
            b"%": self.main_cmd_next_bracket,
            b"\t": self.main_cmd_switch_mode,
            b"{": self.main_k_prev_paragraph,
            b"}": self.main_k_next_paragraph,
            b"x": self.main_cmd_xrefs,
            b"r": self.main_cmd_rename,
            b"I": self.main_cmd_inst_output,
            b"M": self.main_cmd_show_mangling,
            b"B": self.main_cmd_show_bytes,
            b"/": self.main_cmd_search,
            b"n": self.main_cmd_search_forward,
            b"N": self.main_cmd_search_backward,

            b"c": self.main_cmd_set_code,
            b"p": self.main_cmd_set_function,
            b"b": self.main_cmd_set_byte,
            b"w": self.main_cmd_set_word,
            b"d": self.main_cmd_set_dword,
            b"Q": self.main_cmd_set_qword,
            b"a": self.main_cmd_set_ascii,
            b"o": self.main_cmd_set_offset,
            b"*": self.main_cmd_set_array,
            b"U": self.main_cmd_undefine,

            b"\n": self.main_cmd_enter,
            b"\x1b": self.main_cmd_escape,
            # I wanted ctrl-enter but it cannot be mapped on my terminal
            b"u": self.main_cmd_reenter, # u for undo
        }

        self.mapping.update(new_mapping)

        saved_quiet = self.gctx.quiet
        self.gctx.quiet = True

        self.screen = curses.initscr()

        curses.noecho()
        curses.cbreak()
        curses.mousemask(curses.ALL_MOUSE_EVENTS | curses.REPORT_MOUSE_POSITION)

        curses.start_color()
        curses.use_default_colors()

        if self.gctx.color:
            for i in range(0, curses.COLORS):
                curses.init_pair(i, i, -1)

            try:
                curses.init_pair(1, COLOR_SEARCH_FG, COLOR_SEARCH_BG)
            except:
                curses.nocbreak()
                curses.echo()
                curses.endwin()
                self.gctx.quiet = saved_quiet
                error("is your terminal supports 256 colours ? check the issue #58")
                return
        else:
            for i in range(0, curses.COLORS):
                curses.init_pair(i, 7, -1) # white

        (h, w) = self.screen.getmaxyx()
        h -= 1 # status bar

        # Init some y coordinate, used to compute the cursor position
        self.init_section_coords()

        self.win_y = self.dump_update_up(h, self.win_y)
        self.goto_address(ctx.entry, h, w)
        self.main_cmd_line_middle(h, w)

        try:
            curses.wrapper(self.start_view)
        except:
            curses.nocbreak()
            curses.echo()
            curses.endwin()
            self.gctx.quiet = saved_quiet
            traceback.print_exc()
            return

        curses.nocbreak()
        curses.echo()
        curses.endwin()

        self.gctx.quiet = saved_quiet


    def init_section_coords(self):
        self.section_normalized = {}
        self.total_size = 0
        for s in self.api.iter_sections():
            self.section_normalized[s.start] = self.total_size
            self.total_size += s.virt_size


    def get_y_cursor(self, h8, h):
        if self.mode == MODE_DECOMPILE or self.last_curr_line_ad is None:
            return Window.get_y_cursor(self, h8, h)

        ad = self.last_curr_line_ad
        s = self.api.get_section(ad)
        ad_normalized = self.section_normalized[s.start] + ad - s.start
        return int(ad_normalized * h8 / self.total_size)


    def set_last_addr(self):
        ad = self.db.mem.get_head_addr(max(self.output.addr_line))
        ad += self.db.mem.get_size(ad)
        self.last_addr = ad


    def exec_disasm(self, addr, h, dump_until=-1):
        self.ctx = self.gctx.get_addr_context(addr)

        if self.mode == MODE_DUMP:
            if dump_until == -1:
                o = self.ctx.dump_asm()
            else:
                ad = self.db.mem.get_head_addr(dump_until)
                ad += self.db.mem.get_size(ad)
                o = self.ctx.dump_asm(until=ad)

        elif self.mode == MODE_DECOMPILE:
            self.status_bar_message("decompiling...", h, True)
            o = self.ctx.decompile()

        if o is not None:
            self.output = o
            self.token_lines = o.token_lines
            self.set_last_addr()
            self.first_addr = min(o.addr_line)
            return True
        return False


    def status_bar_message(self, s, h, refresh=False):
        self.screen.move(h, 0)
        self.screen.clrtoeol()
        self.screen.addstr(h, 0, s)
        if refresh:
            self.screen.refresh()


    def redraw(self, h, w):
        # Redraw first the statusbar
        if self.has_statusbar:
            self.screen.move(h, 0)
            self.screen.clrtoeol()

        line = self.win_y + self.cursor_y
        if line in self.output.line_addr:
            ad = self.output.line_addr[line]
            self.last_curr_line_ad = ad
        else:
            ad = self.last_curr_line_ad

        if ad is not None:
            s = self.dis.binary.get_section(ad)
            self.screen.addstr(h, 0, s.name, curses.A_BOLD)

            if self.db.mem.is_code(ad):
                fid = self.db.mem.get_func_id(ad)
                if fid != -1:
                    func_ad = self.db.func_id[fid]
                    name = self.api.get_symbol(func_ad)
                    if w - len(name) - 1 < 0:
                        self.screen.insstr(h, 0, name)
                    else:
                        self.screen.insstr(h, w - len(name) - 1, name)

        Window.redraw(self, h, w)


    def main_cmd_rename(self, h, w):
        num_line = self.win_y + self.cursor_y
        line = self.output.lines[num_line]
        if self.cursor_x >= len(line):
            self.cursor_x = len(line) - 1

        word = self.get_word_under_cursor()
        is_var = self.is_tok_var()

        if is_var:
            # Rename a stack variable

            line = self.win_y + self.cursor_y
            while line not in self.output.line_addr:
                line += 1

            fid = self.db.mem.get_func_id(self.output.line_addr[line])
            func_ad = self.db.func_id[fid]

            if word.startswith("var_"):
                try:
                    off = - int(word[word.index("_") + 1:], 16)
                except:
                    return True
                word = ""
            elif word.startswith("arg_"):
                try:
                    off = int(word[word.index("_") + 1:], 16)
                except:
                    return True
                word = ""
            else:
                # Check if word was a renamed variable and get the offset value
                off = self.dis.var_get_offset(func_ad, word)
                if off is None:
                    return True
        else:
            # Rename a symbol

            ad = self.api.get_addr_from_symbol(word)
            if ad == -1:
                return True

            if self.api.is_reserved_prefix(word):
                word = ""

        text = popup_inputbox("rename", word, h, w).replace(" ", "_")

        if word == text or not text or self.api.is_reserved_prefix(text):
            return True

        if is_var:
            self.dis.var_rename(func_ad, off, text)
        else:
            self.api.add_symbol(ad, text)

        self.reload_output(h)
        self.db.modified = True

        return True


    def main_cmd_inst_output(self, h, w):
        # 0 : mnemonics are replaced by something more readdable
        # 1 : print mnemonic but with analyzed oprerands
        # 2 : original capstone string
        if self.gctx.capstone_string == 2:
            self.gctx.capstone_string = 0
        else:
            self.gctx.capstone_string += 1
        self.reload_output(h)
        return True


    def main_cmd_show_bytes(self, h, w):
        self.gctx.print_bytes = not self.gctx.print_bytes
        self.reload_output(h)
        return True


    def main_cmd_show_mangling(self, h, w):
        self.gctx.show_mangling = not self.gctx.show_mangling
        self.reload_output(h)
        return True


    def __search(self, text, h, w, forward=True):
        # Search the next line with an address
        line = self.win_y + self.cursor_y
        moved = False
        while line not in self.output.line_addr:
            line += 1
            moved = True


        # Goto next line
        if forward and not moved:
            line += 1
            while line not in self.output.line_addr:
                line += 1
            ad = self.db.mem.get_head_addr(self.output.line_addr[line])
            ad += self.db.mem.get_size(ad)
        else:
            ad = self.output.line_addr[line]


        s = self.dis.binary.get_section(ad)

        if s is None:
            self.status_bar_message("not found", h, True)
            return False

        while 1:
            off = ad - s.start

            new_off = s.data.find(text, off) if forward \
                      else s.data.rfind(text, 0, off)

            if new_off != -1:
                topush = self.__compute_curr_position()
                self.saved_stack.clear()
                self.stack.append(topush)
                ad = self.db.mem.get_head_addr(s.start + new_off)
                if not self.goto_address(ad, h, w):
                    if self.exec_disasm(ad, h):
                        self.cursor_y = 0
                        self.win_y = 0
                        self.goto_address(self.ctx.entry, h, w)
                return True

            s = self.dis.binary.get_next_section(ad) if forward \
                else self.dis.binary.get_prev_section(ad)

            if s is None:
                self.status_bar_message("not found", h, True)
                return False

            ad = ad = s.start if forward else s.end


    def main_cmd_search(self, h, w):
        self.status_bar_message("/", h, True)
        text = inputbox("", 1, h, w - 1, h)

        if not text:
            return True

        if text[0] == "!":
            textenc = []
            for by in text[1:].split():
                try:
                    textenc.append(int(by, 16))
                except:
                    self.status_bar_message("err search not in hexa", h, True)
                    return False
            textenc = bytes(textenc)

        else:
            textenc = text.encode()

        if self.__search(textenc, h, w, forward=True):
            self.search_bin = textenc
            return True
        return False


    def main_cmd_search_forward(self, h, w):
        if self.search_bin is None:
            return False
        return self.__search(self.search_bin, h, w, forward=True)


    def main_cmd_search_backward(self, h, w):
        if self.search_bin is None:
            return False
        return self.__search(self.search_bin, h, w, forward=False)


    def view_inline_comment_editor(self, h, w):
        line = self.win_y + self.cursor_y
        if line not in self.output.line_addr:
            return True

        addr = self.output.line_addr[line]

        # The same address can be repeated on multiple lines
        # With this we are sure to everytime on the same line
        # example for mips:
        #   # 0x4002bc: lui $gp, 0x19
        #   # 0x4002c0: addiu $gp, $gp, -0x63dc
        #   0x4002bc: li $gp, 0x189c24
        new_line = self.output.addr_line[addr]

        if new_line != line:
            self.goto_line(new_line, h)
            line = new_line
            (h, w) = self.screen.getmaxyx()
            self.view_main_redraw(h, w)

        tok_line = list(self.output.token_lines[line])
        str_line = str(self.output.lines[line])

        # A user comment should always be at the end of the line

        # Get coords of the user comment
        if addr in self.db.user_inline_comments:
            xbegin = self.output.idx_tok_inline_comm[line]
            str_line = str_line[:xbegin]
            tok_line.pop(-1)
            text = self.db.user_inline_comments[addr]
            is_new_token = False
        else:
            tok_line.append((" ; ", COLOR_USER_COMMENT.val,
                    COLOR_USER_COMMENT.bold))
            str_line += " ; "
            xbegin = len(self.output.lines[line]) + 3
            text = ""
            is_new_token = True

        self.status_bar_message("-- INLINE COMMENT --", h)

        idx_token = len(tok_line)
        ed = InlineEd(h, w, line, xbegin, idx_token, text,
                      COLOR_USER_COMMENT.val, tok_line)
        ed.cursor_x = self.cursor_x
        ed.cursor_y = self.cursor_y

        ret = ed.start_view(self.screen)

        if ret:
            self.db.modified = True
            if ed.text:
                self.db.user_inline_comments[addr] = ed.text
                o = (ed.text, COLOR_USER_COMMENT.val, COLOR_USER_COMMENT.bold)
                ed.tok_line.append(o)
                str_line += ed.text

                self.output.lines[line] = str_line
                self.output.token_lines[line] = ed.tok_line
                self.output.idx_tok_inline_comm[line] = xbegin

            else:
                ed.tok_line.pop(-1) # remove the " ; "
                str_line = str_line[:-3]

                self.output.token_lines[line] = ed.tok_line
                self.output.lines[line] = str_line

                if not is_new_token:
                    del self.db.user_inline_comments[addr]

        return True


    def dump_update_up(self, h, wy):
        if self.mode != MODE_DUMP or wy > 10:
            return wy

        if self.first_addr == self.dis.binary.get_first_addr():
            return wy

        # Get an address 256 bytes before, we can't guess the number of lines
        # we wan't to disassemble.
        ad = self.first_addr
        s = self.dis.binary.get_section(ad)
        rest = 256
        while rest:
            ad -= rest
            if ad >= s.start:
                break
            rest = s.start - ad
            start = s.start
            s = self.dis.binary.get_prev_section(start)
            if s is None:
                ad = start
                break
            ad = s.end + 1

        self.ctx = self.gctx.get_addr_context(ad)
        o = self.ctx.dump_asm(until=self.first_addr)

        if o is not None:
            nb_new_lines = len(o.lines)

            if self.win_y == 0 and self.cursor_y < 3:
                if nb_new_lines >= 3:
                    diff = 3 - self.cursor_y
                    self.cursor_y = 3
                    wy -= diff
                    self.win_y -= diff
                else:
                    self.cursor_y = nb_new_lines

            if nb_new_lines >= 3:
                wy += nb_new_lines
                self.win_y += nb_new_lines

            # Update line counter on each line

            self.output.lines = o.lines + self.output.lines
            self.output.token_lines = o.token_lines + self.output.token_lines
            self.first_addr = min(o.addr_line)

            for ad, l in self.output.addr_line.items():
                o.line_addr[nb_new_lines + l] = ad
                o.addr_line[ad] = nb_new_lines + l
                if l in self.output.idx_tok_inline_comm:
                    o.idx_tok_inline_comm[nb_new_lines + l] = \
                            self.output.idx_tok_inline_comm[l]

            self.output.line_addr.clear()
            self.output.addr_line.clear()
            self.output.idx_tok_inline_comm.clear()
            self.output.line_addr = o.line_addr
            self.output.addr_line = o.addr_line
            self.output.idx_tok_inline_comm = o.idx_tok_inline_comm
            self.token_lines = self.output.token_lines

        return wy


    def dump_update_bottom(self, h, wy):
        if self.mode != MODE_DUMP or wy < len(self.token_lines) - h - 10:
            return

        if self.last_addr - 1 == self.dis.binary.get_last_addr():
            return

        ad = self.last_addr

        self.ctx = self.gctx.get_addr_context(ad)
        o = self.ctx.dump_asm()

        if o is not None:
            nb_new_lines = len(self.output.lines)

            self.output.lines += o.lines
            self.output.token_lines += o.token_lines

            for l in o.line_addr:
                self.output.line_addr[nb_new_lines + l] = o.line_addr[l]
                self.output.addr_line[o.line_addr[l]] = nb_new_lines + l
                if l in self.output.idx_tok_inline_comm:
                    self.output.idx_tok_inline_comm[nb_new_lines + l] = \
                            o.idx_tok_inline_comm[l]

            self.set_last_addr()


    # New mappings


    def main_k_prev_paragraph(self, h, w):
        l = self.win_y + self.cursor_y - 1
        while l > 0 and len(self.output.lines[l]) != 0:
            l -= 1
        if l >= 0:
            self.goto_line(l, h)
            self.check_cursor_x()
        return True

    def main_k_next_paragraph(self, h, w):
        l = self.win_y + self.cursor_y + 1
        while l < len(self.output.lines)-1 and len(self.output.lines[l]) != 0:
            l += 1
        if l < len(self.output.lines):
            self.goto_line(l, h)
            self.check_cursor_x()
        return True


    def main_cmd_line_middle(self, h, w):
        mid = int(h/2)
        if self.cursor_y + self.win_y > mid:
            self.win_y += self.cursor_y - mid
            self.cursor_y = mid
        return True


    def main_k_top(self, h, w):
        self.stack.append(self.__compute_curr_position())
        self.saved_stack.clear()

        if self.mode == MODE_DUMP:
            top = self.dis.binary.get_first_addr()
            if self.first_addr != top:
                self.exec_disasm(top, h)

        Window.k_top(self, h, w)
        self.last_curr_line_ad = self.first_addr
        return True


    def main_k_bottom(self, h, w):
        self.stack.append(self.__compute_curr_position())
        self.saved_stack.clear()

        self.cursor_x = 0

        if self.mode == MODE_DUMP:
            bottom = self.dis.binary.get_last_addr()

            if self.last_addr - 1 != bottom:
                self.exec_disasm(bottom, h)
                self.dump_update_up(h, self.win_y)
                self.win_y = 0
                self.cursor_y = 0

        Window.k_bottom(self, h, w)
        self.last_curr_line_ad = self.last_addr - 1
        return True


    def main_cmd_next_bracket(self, h, w):
        # TODO: fix self.cursor_x >= w
        line = self.win_y + self.cursor_y
        line_str = self.output.lines[line]
        x = self.cursor_x
        if x >= len(line_str):
            x = len(line_str) - 1
        char = line_str[x]
        new_line = -1

        if char == "}":
            l = line - 1
            while l >= 0:
                if self.output.lines[l][x] != " ":
                    new_line = l
                    break
                l -= 1

            if l != -1:
                x = 0
                while x < len(self.output.lines[l]):
                    if self.output.lines[l][x] == "{":
                        break
                    x += 1

            self.cursor_x = x

        elif char == "{":
            x = 0
            while 1:
                if self.output.lines[line][x] != " ":
                    break
                x += 1

            l = line + 1
            while l < len(self.output.lines):
                if self.output.lines[l][x] != " ":
                    new_line = l
                    break
                l += 1

            self.cursor_x = x

        if new_line != -1:
            self.goto_line(new_line, h)

        return True


    def __compute_curr_position(self):
        line = self.win_y + self.cursor_y
        if self.mode == MODE_DECOMPILE:
            last = self.ctx.entry
            offset_y = (self.win_y, self.cursor_y)
        else:
            # We save only addresses on the stack, so if the cursor is
            # not on a line with an address, we search the nearest line
            # and save an offset to the original line.
            offset_y = 0
            while line not in self.output.line_addr:
                line += 1
                offset_y += 1
            last = self.output.line_addr[line]
        return (last, self.cursor_x, self.mode, offset_y)


    def main_cmd_enter(self, h, w):
        num_line = self.win_y + self.cursor_y
        line = self.output.lines[num_line]
        if self.cursor_x >= len(line):
            self.cursor_x = len(line) - 1

        word = self.get_word_under_cursor()
        if word is None:
            return False

        topush = self.__compute_curr_position()

        ctx = self.gctx.get_addr_context(word)
        if not ctx:
            return False

        ad = ctx.entry
        self.cursor_x = 0

        if self.goto_address(ad, h, w):
            self.saved_stack.clear()
            self.stack.append(topush)
            return True

        self.ctx = ctx

        if self.mode == MODE_DECOMPILE and not self.db.mem.is_func(ad):
            self.mode = MODE_DUMP

        ret = self.exec_disasm(ad, h)
        if ret:
            self.cursor_y = 0
            self.win_y = 0
            self.saved_stack.clear()
            self.stack.append(topush)
            self.goto_address(ad, h, w)
        return ret


    def __do_go_back(self, args, h, w):
        ad, x, mode, offset_y = args
        self.cursor_x = x

        if mode == MODE_DECOMPILE:
            line = offset_y[0] + offset_y[1]

            if self.mode == MODE_DECOMPILE and ad == self.ctx.entry:
                self.goto_line(line, h)
                self.cursor_x = x
                return True

            self.mode = mode
            ret = self.exec_disasm(ad, h)
            self.goto_line(line, h)

        else:
            if self.mode == MODE_DUMP and self.goto_address(ad, h, w):
                if offset_y != 0:
                    self.scroll_up(h, offset_y, False)
                return True

            self.mode = mode
            ret = self.exec_disasm(ad, h)

            if ret:
                self.goto_address(ad, h, w)
                if offset_y != 0:
                    self.scroll_up(h, offset_y, False)

        return ret


    def main_cmd_escape(self, h, w):
        if not self.stack:
            return False
        poped = self.stack.pop(-1)
        self.saved_stack.append(self.__compute_curr_position())
        return self.__do_go_back(poped, h, w)


    def main_cmd_reenter(self, h, w):
        if not self.saved_stack:
            return False
        poped = self.saved_stack.pop(-1)
        self.stack.append(self.__compute_curr_position())
        return self.__do_go_back(poped, h, w)


    def main_cmd_switch_mode(self, h, w):
        self.stack.append(self.__compute_curr_position())

        # Get a line with an address: the cursor is maybe on a comment
        l = self.win_y + self.cursor_y
        while l not in self.output.line_addr and l <= len(self.token_lines):
            l += 1

        if l not in self.output.line_addr:
            return False

        ad = self.output.line_addr[l]

        if self.mode == MODE_DUMP:
            func_id = self.db.mem.get_func_id(ad)

            if func_id == -1:
                self.status_bar_message("not in a function: create a function or use "
                                "the cmd x in the console", h, True)
                return False

            ad_disasm = self.db.func_id[func_id]
            self.mode = MODE_DECOMPILE

        else:
            ad_disasm = ad
            self.mode = MODE_DUMP

        ret = self.exec_disasm(ad_disasm, h)

        if ret:
            self.cursor_x = 0
            self.win_y = 0
            self.cursor_y = 0
            self.win_y = self.dump_update_up(h, self.win_y)
            self.goto_address(ad, h, w)
            self.main_cmd_line_middle(h, w)

        return ret


    def reload_output(self, h):
        self.exec_disasm(self.first_addr, h, dump_until=self.last_addr-1)


    def main_cmd_set_code(self, h, w):
        if self.mode == MODE_DECOMPILE:
            return False

        line = self.win_y + self.cursor_y
        if line not in self.output.line_addr:
            return False

        ad = self.output.line_addr[line]

        if not self.api.set_code(ad):
            return False

        self.reload_output(h)
        self.goto_address(ad, h, w)
        self.db.modified = True
        return True


    def main_cmd_set_byte(self, h, w):
        line = self.win_y + self.cursor_y
        if line not in self.output.line_addr:
            return False
        ad = self.output.line_addr[line]

        if not self.api.set_byte(ad):
            self.status_bar_message("undefine first", h, True)
            return False

        self.reload_output(h)
        self.db.modified = True
        return True


    def main_cmd_set_word(self, h, w):
        line = self.win_y + self.cursor_y
        if line not in self.output.line_addr:
            return False
        ad = self.output.line_addr[line]

        if not self.api.set_word(ad):
            self.status_bar_message("undefine first", h, True)
            return False

        # TODO: add it for 16 bits
        # we should first check the architecture to not set an
        # offset on 32/64 bits .
        # self.api.set_offset(ad)

        self.reload_output(h)
        self.db.modified = True
        return True


    def main_cmd_set_dword(self, h, w):
        line = self.win_y + self.cursor_y
        if line not in self.output.line_addr:
            return False
        ad = self.output.line_addr[line]

        if not self.api.set_dword(ad):
            self.status_bar_message("undefine first", h, True)
            return False

        # TODO: check architecture first
        self.api.set_offset(ad)

        self.reload_output(h)
        self.db.modified = True
        return True


    def main_cmd_set_qword(self, h, w):
        line = self.win_y + self.cursor_y
        if line not in self.output.line_addr:
            return False
        ad = self.output.line_addr[line]

        if not self.api.set_qword(ad):
            self.status_bar_message("undefine first", h, True)
            return False

        # TODO: check architecture first
        self.api.set_offset(ad)

        self.reload_output(h)
        self.db.modified = True
        return True


    def main_cmd_set_ascii(self, h, w):
        line = self.win_y + self.cursor_y
        if line not in self.output.line_addr:
            return False
        ad = self.output.line_addr[line]

        if not self.api.set_ascii(ad):
            self.status_bar_message("undefine first", h, True)
            return False

        self.reload_output(h)
        self.db.modified = True
        return True


    def main_cmd_set_offset(self, h, w):
        line = self.win_y + self.cursor_y
        if line not in self.output.line_addr:
            return False
        ad = self.output.line_addr[line]

        if not self.api.set_offset(ad):
            self.status_bar_message("not an address", h, True)
            return False

        self.reload_output(h)
        self.db.modified = True
        return True


    def main_cmd_undefine(self, h, w):
        if self.mode == MODE_DECOMPILE:
            return False

        line = self.win_y + self.cursor_y
        if line not in self.output.line_addr:
            return False
        ad = self.output.line_addr[line]
        self.api.undefine(ad)
        self.reload_output(h)
        self.db.modified = True
        return True


    def main_cmd_set_array(self, h, w):
        line = self.win_y + self.cursor_y
        if line not in self.output.line_addr:
            return False
        ad = self.output.line_addr[line]

        main_ty = ty = self.db.mem.get_type(ad)
        if ty == MEM_ARRAY:
            ty = self.db.mem.get_array_entry_type(ad)
            sz_entry = self.db.mem.get_size_from_type(ty)
        else:
            if ty == -1 or ty == MEM_UNK:
                ty = MEM_BYTE
            if ty < MEM_BYTE or ty > MEM_QOFFSET:
                self.status_bar_message("can't create an array here", h, True)
                return False
            sz_entry = self.db.mem.get_size(ad)

        if sz_entry == 1:
            type_name = "byte"
        elif sz_entry == 2:
            type_name = "short"
        elif sz_entry == 4:
            type_name = "int"
        elif sz_entry == 8:
            type_name = "long"

        if main_ty == MEM_ARRAY:
            n = int(self.db.mem.get_size(ad) / sz_entry)
        else:
            # Try to detect the size
            n = 1
            tmp_ad = ad + sz_entry
            s = self.dis.binary.get_section(tmp_ad)
            while tmp_ad <= s.end:
                tmp_ty = self.db.mem.get_type(tmp_ad)
                if tmp_ty != -1 and tmp_ty != MEM_UNK and \
                         (tmp_ty < MEM_BYTE or tmp_ty > MEM_QOFFSET):
                    break
                if tmp_ad in self.db.xrefs:
                    break

                tmp_ad += sz_entry
                n += 1

        word = popup_inputbox("array of %s" % type_name, str(n), h, w)

        if word == "":
            return True

        try:
            n = int(word)
        except:
            self.redraw(h, w)
            self.status_bar_message("error: not an integer", h)
            return False

        self.api.set_array(ad, n, ty)
        self.reload_output(h)
        self.db.modified = True
        return True


    def main_cmd_set_function(self, h, w):
        if self.mode == MODE_DECOMPILE:
            return False

        line = self.win_y + self.cursor_y
        if line not in self.output.line_addr:
            return False

        ad = self.output.line_addr[line]

        if not self.api.set_function(ad):
            self.status_bar_message("cannot set a function here", h, True)
            return False

        self.reload_output(h)
        self.db.modified = True
        self.goto_address(ad, h, w)
        return True


    def main_cmd_xrefs(self, h, w):
        num_line = self.win_y + self.cursor_y
        line = self.output.lines[num_line]
        if self.cursor_x >= len(line):
            self.cursor_x = len(line) - 1

        word = self.get_word_under_cursor()
        if word is None:
            return None

        ctx = self.gctx.get_addr_context(word)
        if ctx is None:
            self.status_bar_message("unknown symbol", h, True)
            return False

        if not ctx or (ctx.entry not in self.db.xrefs and
                (ctx.entry not in self.db.mem.data_sub_xrefs or
                not self.db.mem.data_sub_xrefs[ctx.entry])):
            self.status_bar_message("no xrefs", h, True)
            return False

        o = ctx.dump_xrefs()
        (ret, line) = popup_text("xrefs for 0x%x" % ctx.entry, o, h, w)

        if not ret:
            return True

        # Goto the selected xref

        ad = o.line_addr[line]
        topush = self.__compute_curr_position()
        self.cursor_x = 0

        if self.goto_address(ad, h, w):
            self.saved_stack.clear()
            self.stack.append(topush)
            return True

        ad_disasm = ad

        if self.mode == MODE_DECOMPILE:
            func_id = self.db.mem.get_func_id(ad)
            if func_id == -1:
                self.mode = MODE_DUMP
            else:
                ad_disasm = self.db.func_id[func_id]

        ret = self.exec_disasm(ad_disasm, h)
        if ret:
            self.cursor_y = 0
            self.win_y = 0
            self.saved_stack.clear()
            self.stack.append(topush)
            self.win_y = self.dump_update_up(h, self.win_y)
            self.main_cmd_line_middle(h, w)
            self.goto_address(ad, h, w)
        return ret


    def mouse_double_left_click(self, h, w):
        return self.main_cmd_enter(h, w)
