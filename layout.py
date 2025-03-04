import heapq
import logging
import re
from .utils import INF
from .utils import Plane
from .utils import apply_matrix_pt
from .utils import bbox2str
from .utils import fsplit
from .utils import get_bound
from .utils import matrix2str
from .utils import uniq
import datetime
from dateutil import parser
from pathlib import Path
import pandas as pd
from fuzzywuzzy import fuzz, process, utils
logger = logging.getLogger(__name__)

mappings_file_path = Path(__file__).parent/"Mapping_BS_fields.csv"


class IndexAssigner:
    def __init__(self, index=0):
        self.index = index
        return

    def run(self, obj):
        if isinstance(obj, LTTextBox):
            obj.index = self.index
            self.index += 1
        elif isinstance(obj, LTTextGroup):
            for x in obj:
                self.run(x)
        return


class LAParams:
    """Parameters for layout analysis

    :param line_overlap: If two characters have more overlap than this they
        are considered to be on the same line. The overlap is specified
        relative to the minimum height of both characters.
    :param char_margin: If two characters are closer together than this
        margin they are considered part of the same line. The margin is
        specified relative to the width of the character.
    :param word_margin: If two characters on the same line are further apart
        than this margin then they are considered to be two separate words, and
        an intermediate space will be added for readability. The margin is
        specified relative to the width of the character.
    :param line_margin: If two lines are are close together they are
        considered to be part of the same paragraph. The margin is
        specified relative to the height of a line.
    :param boxes_flow: Specifies how much a horizontal and vertical position
        of a text matters when determining the order of text boxes. The value
        should be within the range of -1.0 (only horizontal position
        matters) to +1.0 (only vertical position matters). You can also pass
        `None` to disable advanced layout analysis, and instead return text
        based on the position of the bottom left corner of the text box.
    :param detect_vertical: If vertical text should be considered during
        layout analysis
    :param all_texts: If layout analysis should be performed on text in
        figures.
    """

    def __init__(self,
                 line_overlap=0.5,
                 char_margin=2.0,
                 line_margin=0.5,
                 word_margin=0.1,
                 boxes_flow=0.5,
                 char_margin_for_word=0.05,
                 detect_vertical=False,
                 all_texts=False):
        self.line_overlap = line_overlap
        self.char_margin = char_margin
        self.line_margin = line_margin
        self.word_margin = word_margin
        self.boxes_flow = boxes_flow
        self.detect_vertical = detect_vertical
        self.all_texts = all_texts
        self.char_margin_for_word = char_margin_for_word

        self._validate()
        return

    def _validate(self):
        if self.boxes_flow is not None:
            boxes_flow_err_msg = ("LAParam boxes_flow should be None, or a "
                                  "number between -1 and +1")
            if not (isinstance(self.boxes_flow, int)
                    or isinstance(self.boxes_flow, float)):
                raise TypeError(boxes_flow_err_msg)
            if not -1 <= self.boxes_flow <= 1:
                raise ValueError(boxes_flow_err_msg)

    def __repr__(self):
        return '<LAParams: char_margin=%.1f, line_margin=%.1f, ' \
               'word_margin=%.1f all_texts=%r>' % \
               (self.char_margin, self.line_margin, self.word_margin,
                self.all_texts)


class LTItem:
    """Interface for things that can be analyzed"""

    def analyze(self, laparams):
        """Perform the layout analysis."""
        return


class LTText:
    """Interface for things that have text"""

    def __repr__(self):
        return ('<%s %r>' % (self.__class__.__name__, self.get_text()))

    def get_text(self):
        """Text contained in this object"""
        raise NotImplementedError


class LTComponent(LTItem):
    """Object with a bounding box"""

    def __init__(self, bbox):
        LTItem.__init__(self)
        bbox = tuple(map(lambda x: round(x, 2), list(bbox)))
        self.set_bbox(bbox)
        return

    def __repr__(self):
        return ('<%s %s>' % (self.__class__.__name__, bbox2str(self.bbox)))

    # Disable comparison.
    def __lt__(self, _):
        raise ValueError

    def __le__(self, _):
        raise ValueError

    def __gt__(self, _):
        raise ValueError

    def __ge__(self, _):
        raise ValueError

    def set_bbox(self, bbox):
        (x0, y0, x1, y1) = bbox
        self.x0 = x0
        self.y0 = y0
        self.x1 = x1
        self.y1 = y1
        self.width = x1 - x0
        self.height = y1 - y0
        self.bbox = bbox
        return

    def is_empty(self):
        return self.width <= 0 or self.height <= 0

    def is_hoverlap(self, obj):
        assert isinstance(obj, LTComponent), str(type(obj))
        return obj.x0 <= self.x1 and self.x0 <= obj.x1

    def hdistance(self, obj):
        assert isinstance(obj, LTComponent), str(type(obj))
        if self.is_hoverlap(obj):
            return 0
        else:
            return min(abs(self.x0 - obj.x1), abs(self.x1 - obj.x0))

    def hoverlap(self, obj):
        assert isinstance(obj, LTComponent), str(type(obj))
        if self.is_hoverlap(obj):
            return min(abs(self.x0 - obj.x1), abs(self.x1 - obj.x0))
        else:
            return 0

    def is_voverlap(self, obj):
        assert isinstance(obj, LTComponent), str(type(obj))
        return obj.y0 <= self.y1 and self.y0 <= obj.y1

    def vdistance(self, obj):
        assert isinstance(obj, LTComponent), str(type(obj))
        if self.is_voverlap(obj):
            return 0
        else:
            return min(abs(self.y0 - obj.y1), abs(self.y1 - obj.y0))

    def voverlap(self, obj):
        assert isinstance(obj, LTComponent), str(type(obj))
        if self.is_voverlap(obj):
            return min(abs(self.y0 - obj.y1), abs(self.y1 - obj.y0))
        else:
            return 0


class LTCurve(LTComponent):
    """A generic Bezier curve"""

    def __init__(self,
                 linewidth,
                 pts,
                 stroke=False,
                 fill=False,
                 evenodd=False,
                 stroking_color=None,
                 non_stroking_color=None):
        LTComponent.__init__(self, get_bound(pts))
        self.pts = pts
        self.linewidth = linewidth
        self.stroke = stroke
        self.fill = fill
        self.evenodd = evenodd
        self.stroking_color = stroking_color
        self.non_stroking_color = non_stroking_color
        return

    def get_pts(self):
        return ','.join('%.3f,%.3f' % p for p in self.pts)


class LTLine(LTCurve):
    """A single straight line.

    Could be used for separating text or figures.
    """

    def __init__(self,
                 linewidth,
                 p0,
                 p1,
                 stroke=False,
                 fill=False,
                 evenodd=False,
                 stroking_color=None,
                 non_stroking_color=None):
        LTCurve.__init__(self, linewidth, [p0, p1], stroke, fill, evenodd,
                         stroking_color, non_stroking_color)
        return


class LTRect(LTCurve):
    """A rectangle.

    Could be used for framing another pictures or figures.
    """

    def __init__(self,
                 linewidth,
                 bbox,
                 stroke=False,
                 fill=False,
                 evenodd=False,
                 stroking_color=None,
                 non_stroking_color=None):
        (x0, y0, x1, y1) = bbox
        LTCurve.__init__(self, linewidth, [(x0, y0), (x1, y0), (x1, y1),
                                           (x0, y1)], stroke, fill, evenodd,
                         stroking_color, non_stroking_color)
        return


class LTImage(LTComponent):
    """An image object.

    Embedded images can be in JPEG, Bitmap or JBIG2.
    """

    def __init__(self, name, stream, bbox):
        LTComponent.__init__(self, bbox)
        self.name = name
        self.stream = stream
        self.srcsize = (stream.get_any(
            ('W', 'Width')), stream.get_any(('H', 'Height')))
        self.imagemask = stream.get_any(('IM', 'ImageMask'))
        self.bits = stream.get_any(('BPC', 'BitsPerComponent'), 1)
        self.colorspace = stream.get_any(('CS', 'ColorSpace'))
        if not isinstance(self.colorspace, list):
            self.colorspace = [self.colorspace]
        return

    def __repr__(self):
        return ('<%s(%s) %s %r>' % (self.__class__.__name__, self.name,
                                    bbox2str(self.bbox), self.srcsize))


class LTAnno(LTItem, LTText):
    """Actual letter in the text as a Unicode string.

    Note that, while a LTChar object has actual boundaries, LTAnno objects does
    not, as these are "virtual" characters, inserted by a layout analyzer
    according to the relationship between two characters (e.g. a space).
    """

    def __init__(self, text):
        self._text = text
        return

    def get_text(self):
        return self._text


class LTChar(LTComponent, LTText):
    """Actual letter in the text as a Unicode string."""

    def __init__(self, matrix, font, fontsize, scaling, rise, text, textwidth,
                 textdisp, ncs, graphicstate):
        LTText.__init__(self)
        self._text = text
        self.matrix = matrix
        self.fontname = font.fontname
        self.ncs = ncs
        self.graphicstate = graphicstate
        self.adv = textwidth * fontsize * scaling
        # compute the boundary rectangle.
        if font.is_vertical():
            # vertical
            (vx, vy) = textdisp
            if vx is None:
                vx = fontsize * 0.5
            else:
                vx = vx * fontsize * .001
            vy = (1000 - vy) * fontsize * .001
            bbox_lower_left = (-vx, vy + rise + self.adv)
            bbox_upper_right = (-vx + fontsize, vy + rise)
        else:
            # horizontal
            descent = font.get_descent() * fontsize
            bbox_lower_left = (0, descent + rise)
            bbox_upper_right = (self.adv, descent + rise + fontsize)
        (a, b, c, d, e, f) = self.matrix
        self.upright = (0 < a * d * scaling and b * c <= 0)
        (x0, y0) = apply_matrix_pt(self.matrix, bbox_lower_left)
        (x1, y1) = apply_matrix_pt(self.matrix, bbox_upper_right)
        if x1 < x0:
            (x0, x1) = (x1, x0)
        if y1 < y0:
            (y0, y1) = (y1, y0)
        LTComponent.__init__(self, (x0, y0, x1, y1))
        if font.is_vertical():
            self.size = round(self.width, 2)
        else:
            self.size = round(self.height, 2)
        return

    def __repr__(self):
        return ('<%s %s matrix=%s font=%r adv=%s text=%r>' %
                (self.__class__.__name__, bbox2str(self.bbox),
                 matrix2str(
                     self.matrix), self.fontname, self.adv, self.get_text()))

    def get_text(self):
        return self._text

    def is_compatible(self, obj):
        """Returns True if two characters can coexist in the same line."""
        return True


class LTContainer(LTComponent):
    """Object that can be extended and analyzed"""

    def __init__(self, bbox):
        LTComponent.__init__(self, bbox)
        self._objs = []
        return

    def __iter__(self):
        return iter(self._objs)

    def __len__(self):
        return len(self._objs)

    def add(self, obj):
        self._objs.append(obj)
        return

    def extend(self, objs):
        for obj in objs:
            self.add(obj)
        return

    def analyze(self, laparams):
        for obj in self._objs:
            obj.analyze(laparams)
        return

    def is_date(self, date_string):

        regex = re.compile(':')
        if "cr" in date_string.lower() or "dr" in date_string.lower() or date_string.lower().startswith(("/", "-", ":", ";", ",")):
            return None
        try:
            check = float(date_string)
            return None
        except:
            pass

        if regex.search(date_string) != None:
            return None

        if len(date_string) < 6 or len(date_string) > 12:
            return None
        date_string = str(date_string)
        if not date_string:
            return None
        if "-" or "/" in date_string:
            # "date_with_special_chars"

            date_string = date_string.replace(" ", "")
            date_string = date_string.replace("/", "-")
        else:
            date_type = "date_in_english"
            # Replace some similar numbers in OCR
        try:
            input_date = parser.parse(date_string, dayfirst=True)
            input_date = str(input_date).split()[0]
            return input_date
        except:
            # input_date = parser.parse(date_string)
            return None

    def is_header(self, input):
        mappings = pd.read_csv(mappings_file_path)
        mappings["small_Key"] = mappings["Key"].str.lower()
        mappings["small_Key"] = mappings["small_Key"].str.replace(" ", "")

        if input.lower() in list(mappings["small_Key"]):
            return True

        return False

    def is_cl_bal(self, input):
        input = input.replace("\n", "")

        if input == "" or not (utils.full_process(input)):
            return False
        closing_bal_options = ["closingbalance",
                               "closing bal", "clbal", "closing balance"]
        highest_ratio = process.extractOne(
            input.lower(), closing_bal_options)[1]

        if highest_ratio >= 95:

            return True
        return False

    def is_account_number(self, input):
        input = input.replace("\n", "")

        if input == "" or not (utils.full_process(input)):
            return False
        acc_nun_options = ["account number", "account",
                           "acc num", "acc no", "accountno", "a/c", "a/cno", "a/cnum", "a/cnumber"]
        highest_ratio = process.extractOne(input.lower(), acc_nun_options)[1]

        if highest_ratio >= 95:

            return True
        return False

    def is_ac_num_regex(self, input):
        ac_num_regex = "[1-9][0-9]{9,18}"
        input = input.replace("/n", "")
        if re.search(ac_num_regex, input.lower()) != None:
            return True
        return False


class LTExpandableContainer(LTContainer):
    def __init__(self):
        LTContainer.__init__(self, (+INF, +INF, -INF, -INF))
        return

    def add(self, obj):
        LTContainer.add(self, obj)
        self.set_bbox((min(self.x0, obj.x0), min(self.y0, obj.y0),
                       max(self.x1, obj.x1), max(self.y1, obj.y1)))
        return


class LTTextContainer(LTExpandableContainer, LTText):
    def __init__(self):
        LTText.__init__(self)
        LTExpandableContainer.__init__(self)
        return

    def get_text(self):
        return ''.join(obj.get_text() for obj in self
                       if isinstance(obj, LTText))


class LTTextWord(LTTextContainer):
    """Contains a list of LTChar objects that represent a single text line.

    The characters are aligned either horizontally or vertically, depending on
    the text's writing mode.
    """

    def __init__(self, char_margin_for_word):
        LTTextContainer.__init__(self)
        self.char_margin_for_word = char_margin_for_word
        self.fontname = ""
        self.fontsize = 0
        self.num_of_chars = 0
        self.nature = ""
        self.type = ""
        self.form_field = ""
        return

    def __repr__(self):
        return (
            '<%s %s %r>' %
            (self.__class__.__name__, bbox2str(self.bbox), self.get_text()))

    def analyze(self, laparams):
        LTTextContainer.analyze(self, laparams)
        # LTContainer.add(self, LTAnno('\n'))
        return

    def find_neighbors(self, plane, ratio):
        raise NotImplementedError

    def is_compatible(self, obj):
        """Returns True if two characters can coexist in the same line."""
        return True


class LTTextWordHorizontal(LTTextWord):
    def __init__(self, char_margin_for_word):
        LTTextWord.__init__(self, char_margin_for_word)
        self._x1 = +INF
        return

    def add(self, obj):
        # if isinstance(obj, LTChar):
        # margin = self.char_margin * max(obj.width, obj.height)
        # if self._x1 < obj.x0 - margin:
        # LTContainer.add(self, LTAnno(' '))
        self._x1 = obj.x1
        self.num_of_chars += 1

        if self.num_of_chars <= 2:
            self.fontsize = round(obj.size, 1)

        if self.fontname == "":
            self.fontname = obj.fontname

        if self.nature == "" and obj.get_text() == ":":
            self.nature = "form"

        LTTextWord.add(self, obj)
        return

    def _is_left_aligned_with(self, other, tolerance=0):
        """
        Whether the left-hand edge of `other` is within `tolerance`.
        """
        return abs(other.x0 - self.x0) <= tolerance

    def _is_right_aligned_with(self, other, tolerance=0):
        """
        Whether the right-hand edge of `other` is within `tolerance`.
        """
        return abs(other.x1 - self.x1) <= tolerance

    def _is_centrally_aligned_with(self, other, tolerance=0):
        """
        Whether the horizontal center of `other` is within `tolerance`.
        """
        return abs((other.x0 + other.x1) / 2 -
                   (self.x0 + self.x1) / 2) <= tolerance

    def _is_same_height_as(self, other, tolerance):
        return abs(other.height - self.height) <= tolerance


class LTTextWordVertical(LTTextWord):
    def __init__(self, char_margin_for_word):
        LTTextWord.__init__(self, char_margin_for_word)
        self._y0 = -INF
        return

    def add(self, obj):
        # if isinstance(obj, LTChar) and self.word_margin:
        #     margin = self.word_margin * max(obj.width, obj.height)
        #     if obj.y1 + margin < self._y0:
        #         LTContainer.add(self, LTAnno(' '))
        self._y0 = obj.y0
        self.num_of_chars += 1
        if self.num_of_chars <= 2:
            self.fontsize = round(obj.size, 1)
        if self.fontname == "":
            self.fontname = obj.fontname
        LTTextWord.add(self, obj)
        return

    def find_neighbors(self, plane, ratio):
        """
        Finds neighboring LTTextLineVerticals in the plane.

        Returns a list of other LTTextLineVerticals in the plane which are
        close to self. "Close" can be controlled by ratio. The returned objects
        will be the same width as self, and also either upper-, lower-, or
        centrally-aligned.
        """
        d = ratio * self.width
        objs = plane.find((self.x0 - d, self.y0, self.x1 + d, self.y1))
        return [
            obj for obj in objs
            if (isinstance(obj, LTTextLineVertical)
                and self._is_same_width_as(obj, tolerance=d) and (
                    self._is_lower_aligned_with(obj, tolerance=d)
                    or self._is_upper_aligned_with(obj, tolerance=d)
                    or self._is_centrally_aligned_with(obj, tolerance=d)))
        ]

    def _is_lower_aligned_with(self, other, tolerance=0):
        """
        Whether the lower edge of `other` is within `tolerance`.
        """
        return abs(other.y0 - self.y0) <= tolerance

    def _is_upper_aligned_with(self, other, tolerance=0):
        """
        Whether the upper edge of `other` is within `tolerance`.
        """
        return abs(other.y1 - self.y1) <= tolerance

    def _is_centrally_aligned_with(self, other, tolerance=0):
        """
        Whether the vertical center of `other` is within `tolerance`.
        """
        return abs((other.y0 + other.y1) / 2 -
                   (self.y0 + self.y1) / 2) <= tolerance

    def _is_same_width_as(self, other, tolerance):
        return abs(other.width - self.width) <= tolerance


class LTTextLine(LTTextContainer):
    """Contains a list of LTChar objects that represent a single text line.

    The characters are aligned either horizontally or vertically, depending on
    the text's writing mode.
    """

    def __init__(self, word_margin):
        LTTextContainer.__init__(self)
        self.word_margin = word_margin
        self.fontsize = 0
        self.fontname = ""
        self.nature = ""
        self.type = ""
        self.form_field = ""
        return

    def __repr__(self):
        return (
            '<%s %s %r>' %
            (self.__class__.__name__, bbox2str(self.bbox), self.get_text()))

    def analyze(self, laparams):
        LTTextContainer.analyze(self, laparams)
        LTContainer.add(self, LTAnno('\n'))
        return

    def find_neighbors(self, plane, ratio):
        raise NotImplementedError


class LTTextLineHorizontal(LTTextLine):
    def __init__(self, word_margin):
        LTTextLine.__init__(self, word_margin)
        self._x1 = +INF
        return

    def add(self, obj):
        if isinstance(obj, LTChar) and self.word_margin:
            margin = self.word_margin * max(obj.width, obj.height)
            if self._x1 < obj.x0 - margin:
                LTContainer.add(self, LTAnno(' '))
        self._x1 = obj.x1

        if self.nature == "" and obj.nature == "form":
            self.nature = "form"

        if self.is_date(obj.get_text()):
            obj.type = "date"
        if self.is_header(obj.get_text()):
            obj.type = "header"
        if self.is_cl_bal(self.get_text() + obj.get_text()):
            self.form_field = "closing_balance"

        if self.is_account_number(self.get_text() + obj.get_text()):
            self.form_field = "account_number"
        if self.is_account_number(obj.get_text()):
            obj.form_field = "account_number"
        if self.is_ac_num_regex(obj.get_text()):
            obj.form_field = "ac_num_regex"

        if self.fontsize < obj.fontsize:
            self.fontsize = obj.fontsize
        if self.fontname == "":
            self.fontname = obj.fontname
        LTTextLine.add(self, obj)
        return

    def find_neighbors(self, plane, ratio):
        """
        Finds neighboring LTTextLineHorizontals in the plane.

        Returns a list of other LTTestLineHorizontals in the plane which are
        close to self. "Close" can be controlled by ratio. The returned objects
        will be the same height as self, and also either left-, right-, or
        centrally-aligned.
        """
        custom_ratio = 0.25
        d = custom_ratio * self.height
        objs = plane.find((self.x0, self.y0 - d, self.x1, self.y1 + d))
        return [
            obj for obj in objs
            if (isinstance(obj, LTTextLineHorizontal)
                and self._is_same_height_as(obj, tolerance=d) and (
                    self._is_left_aligned_with(obj, tolerance=d)
                    or self._is_right_aligned_with(obj, tolerance=d)
                    or self._is_centrally_aligned_with(obj, tolerance=d)))
        ]

    def _is_left_aligned_with(self, other, tolerance=0):
        """
        Whether the left-hand edge of `other` is within `tolerance`.
        """
        return abs(other.x0 - self.x0) <= tolerance

    def _is_right_aligned_with(self, other, tolerance=0):
        """
        Whether the right-hand edge of `other` is within `tolerance`.
        """
        return abs(other.x1 - self.x1) <= tolerance

    def _is_centrally_aligned_with(self, other, tolerance=0):
        """
        Whether the horizontal center of `other` is within `tolerance`.
        """
        return abs((other.x0 + other.x1) / 2 -
                   (self.x0 + self.x1) / 2) <= tolerance

    def _is_same_height_as(self, other, tolerance):
        return abs(other.height - self.height) <= tolerance


class LTTextLineVertical(LTTextLine):
    def __init__(self, word_margin):
        LTTextLine.__init__(self, word_margin)
        self._y0 = -INF
        return

    def add(self, obj):
        if isinstance(obj, LTChar) and self.word_margin:
            margin = self.word_margin * max(obj.width, obj.height)
            if obj.y1 + margin < self._y0:
                LTContainer.add(self, LTAnno(' '))
        self._y0 = obj.y0

        if self.nature == "" and obj.nature == "form":
            self.nature = "form"

        if self.is_date(obj.get_text()):
            obj.type = "date"
        if self.is_header(obj.get_text()):
            obj.type = "header"
        if self.is_cl_bal(obj.get_text()):
            obj.form_field = "closing_balance"
        if self.is_account_number(obj.get_text()):
            obj.form_field = "account_number"

        if self.fontsize < obj.fontsize:
            self.fontsize = obj.fontsize
        if self.fontname == "":
            self.fontname = obj.fontname

        LTTextLine.add(self, obj)
        return

    def find_neighbors(self, plane, ratio):
        """
        Finds neighboring LTTextLineVerticals in the plane.

        Returns a list of other LTTextLineVerticals in the plane which are
        close to self. "Close" can be controlled by ratio. The returned objects
        will be the same width as self, and also either upper-, lower-, or
        centrally-aligned.
        """
        d = ratio * self.width
        objs = plane.find((self.x0 - d, self.y0, self.x1 + d, self.y1))
        return [
            obj for obj in objs
            if (isinstance(obj, LTTextLineVertical)
                and self._is_same_width_as(obj, tolerance=d) and (
                    self._is_lower_aligned_with(obj, tolerance=d)
                    or self._is_upper_aligned_with(obj, tolerance=d)
                    or self._is_centrally_aligned_with(obj, tolerance=d)))
        ]

    def _is_lower_aligned_with(self, other, tolerance=0):
        """
        Whether the lower edge of `other` is within `tolerance`.
        """
        return abs(other.y0 - self.y0) <= tolerance

    def _is_upper_aligned_with(self, other, tolerance=0):
        """
        Whether the upper edge of `other` is within `tolerance`.
        """
        return abs(other.y1 - self.y1) <= tolerance

    def _is_centrally_aligned_with(self, other, tolerance=0):
        """
        Whether the vertical center of `other` is within `tolerance`.
        """
        return abs((other.y0 + other.y1) / 2 -
                   (self.y0 + self.y1) / 2) <= tolerance

    def _is_same_width_as(self, other, tolerance):
        return abs(other.width - self.width) <= tolerance


class LTTextBox(LTTextContainer):
    """Represents a group of text chunks in a rectangular area.

    Note that this box is created by geometric analysis and does not
    necessarily represents a logical boundary of the text. It contains a list
    of LTTextLine objects.
    """

    def __init__(self):
        LTTextContainer.__init__(self)
        self.index = -1
        self.type = ""
        self.form_field = ""
        return

    def __repr__(self):
        return ('<%s(%s) %s %r>' % (self.__class__.__name__, self.index,
                                    bbox2str(self.bbox), self.get_text()))

    def get_text(self):
        return ' '.join(obj.get_text() for obj in self
                        if isinstance(obj, LTText))


class LTTextBoxHorizontal(LTTextBox):
    def analyze(self, laparams):
        LTTextBox.analyze(self, laparams)
        self._objs.sort(key=lambda obj: -obj.y1)
        return

    def get_writing_mode(self):
        return 'lr-tb'


class LTTextBoxVertical(LTTextBox):
    def analyze(self, laparams):
        LTTextBox.analyze(self, laparams)
        self._objs.sort(key=lambda obj: -obj.x1)
        return

    def get_writing_mode(self):
        return 'tb-rl'


class LTTextGroup(LTTextContainer):
    def __init__(self, objs):
        LTTextContainer.__init__(self)
        self.extend(objs)
        return


class LTTextGroupLRTB(LTTextGroup):
    def analyze(self, laparams):
        LTTextGroup.analyze(self, laparams)
        # reorder the objects from top-left to bottom-right.
        self._objs.sort(key=lambda obj: (1 - laparams.boxes_flow) * obj.x0 -
                        (1 + laparams.boxes_flow) * (obj.y0 + obj.y1))
        return


class LTTextGroupTBRL(LTTextGroup):
    def analyze(self, laparams):
        LTTextGroup.analyze(self, laparams)
        # reorder the objects from top-right to bottom-left.
        self._objs.sort(key=lambda obj: -(1 + laparams.boxes_flow) *
                        (obj.x0 + obj.x1) - (1 - laparams.boxes_flow) * obj.y1)
        return


class LTLayoutContainer(LTContainer):
    def __init__(self, bbox):
        LTContainer.__init__(self, bbox)
        self.groups = None
        return

    # group_objects: group text object to textlines.

    def group_objects(self, laparams, objs):
        obj0 = None
        line = None
        for obj1 in objs:
            if obj0 is not None:
                # halign: obj0 and obj1 is horizontally aligned.
                #
                #   +------+ - - -
                #   | obj0 | - - +------+   -
                #   |      |     | obj1 |   | (line_overlap)
                #   +------+ - - |      |   -
                #          - - - +------+
                #
                #          |<--->|
                #        (char_margin)
                halign = \
                    obj0.is_compatible(obj1) \
                    and obj0.is_voverlap(obj1) \
                    and min(obj0.height, obj1.height) * laparams.line_overlap \
                    < obj0.voverlap(obj1) \
                    and obj0.hdistance(obj1) \
                    < min(obj0.width, obj1.width) * (laparams.char_margin/3)

                # valign: obj0 and obj1 is vertically aligned.
                #
                #   +------+
                #   | obj0 |
                #   |      |
                #   +------+ - - -
                #     |    |     | (char_margin)
                #     +------+ - -
                #     | obj1 |
                #     |      |
                #     +------+
                #
                #     |<-->|
                #   (line_overlap)
                valign = \
                    laparams.detect_vertical \
                    and obj0.is_compatible(obj1) \
                    and obj0.is_hoverlap(obj1) \
                    and min(obj0.width, obj1.width) * laparams.line_overlap \
                    < obj0.hoverlap(obj1) \
                    and obj0.vdistance(obj1) \
                    < max(obj0.height, obj1.height) * (laparams.char_margin/3)

                if ((halign and isinstance(line, LTTextLineHorizontal))
                        or (valign and isinstance(line, LTTextLineVertical))):

                    line.add(obj1)
                elif line is not None:
                    yield line
                    line = None
                else:
                    if valign and not halign:
                        line = LTTextLineVertical(laparams.word_margin)
                        line.add(obj0)
                        line.add(obj1)
                    elif halign and not valign:
                        line = LTTextLineHorizontal(laparams.word_margin)
                        line.add(obj0)
                        line.add(obj1)
                    else:
                        line = LTTextLineHorizontal(laparams.word_margin)
                        line.add(obj0)
                        yield line
                        line = None
            obj0 = obj1
        if line is None:
            line = LTTextLineHorizontal(laparams.word_margin)
            line.add(obj0)

        yield line
        return

    def group_textlines(self, laparams, lines):
        """Group neighboring lines to textboxes"""
        plane = Plane(self.bbox)

        plane.extend(lines)
        boxes = {}
        for line in lines:
            neighbors = line.find_neighbors(plane, laparams.line_margin)
            members = [line]
            for obj1 in neighbors:
                members.append(obj1)
                if obj1 in boxes:
                    members.extend(boxes.pop(obj1))
            if isinstance(line, LTTextLineHorizontal):
                box = LTTextBoxHorizontal()
            else:
                box = LTTextBoxVertical()
            for obj in uniq(members):
                box.add(obj)
                boxes[obj] = box
        done = set()
        for line in lines:
            if line not in boxes:
                continue
            box = boxes[line]
            if box in done:
                continue
            done.add(box)
            if not box.is_empty():
                yield box
        return

    def group_textboxes(self, laparams, boxes):
        """Group textboxes hierarchically.

        Get pair-wise distances, via dist func defined below, and then merge
        from the closest textbox pair. Once obj1 and obj2 are merged /
        grouped, the resulting group is considered as a new object, and its
        distances to other objects & groups are added to the process queue.

        For performance reason, pair-wise distances and object pair info are
        maintained in a heap of (idx, dist, id(obj1), id(obj2), obj1, obj2)
        tuples. It ensures quick access to the smallest element. Note that
        since comparison operators, e.g., __lt__, are disabled for
        LTComponent, id(obj) has to appear before obj in element tuples.

        :param laparams: LAParams object.
        :param boxes: All textbox objects to be grouped.
        :return: a list that has only one element, the final top level textbox.
        """
        def dist(obj1, obj2):
            """A distance function between two TextBoxes.

            Consider the bounding rectangle for obj1 and obj2.
            Return its area less the areas of obj1 and obj2,
            shown as 'www' below. This value may be negative.
                    +------+..........+ (x1, y1)
                    | obj1 |wwwwwwwwww:
                    +------+www+------+
                    :wwwwwwwwww| obj2 |
            (x0, y0) +..........+------+
            """
            x0 = min(obj1.x0, obj2.x0)
            y0 = min(obj1.y0, obj2.y0)
            x1 = max(obj1.x1, obj2.x1)
            y1 = max(obj1.y1, obj2.y1)
            return (x1 - x0) * (y1 - y0) \
                - obj1.width*obj1.height - obj2.width*obj2.height

        def isany(obj1, obj2):
            """Check if there's any other object between obj1 and obj2."""
            x0 = min(obj1.x0, obj2.x0)
            y0 = min(obj1.y0, obj2.y0)
            x1 = max(obj1.x1, obj2.x1)
            y1 = max(obj1.y1, obj2.y1)
            objs = set(plane.find((x0, y0, x1, y1)))
            return objs.difference((obj1, obj2))

        dists = []
        for i in range(len(boxes)):
            obj1 = boxes[i]
            for j in range(i + 1, len(boxes)):
                obj2 = boxes[j]
                dists.append(
                    (False, dist(obj1, obj2), id(obj1), id(obj2), obj1, obj2))
        heapq.heapify(dists)

        plane = Plane(self.bbox)
        plane.extend(boxes)
        done = set()
        while len(dists) > 0:
            (skip_isany, d, id1, id2, obj1, obj2) = heapq.heappop(dists)
            # Skip objects that are already merged
            if (id1 not in done) and (id2 not in done):
                if skip_isany and isany(obj1, obj2):
                    heapq.heappush(dists, (True, d, id1, id2, obj1, obj2))
                    continue
                if isinstance(obj1, (LTTextBoxVertical, LTTextGroupTBRL)) or \
                        isinstance(obj2, (LTTextBoxVertical, LTTextGroupTBRL)):
                    group = LTTextGroupTBRL([obj1, obj2])
                else:
                    group = LTTextGroupLRTB([obj1, obj2])
                plane.remove(obj1)
                plane.remove(obj2)
                done.update([id1, id2])

                for other in plane:
                    heapq.heappush(dists, (False, dist(
                        group, other), id(group), id(other), group, other))
                plane.add(group)
        return list(plane)

    def group_textchars(self, laparams, objs):
        obj0 = None
        word = None
        for obj1 in objs:
            if obj0 is not None:
                # halign: obj0 and obj1 is horizontally aligned.
                #
                #   +------+ - - -
                #   | obj0 | - - +------+   -
                #   |      |     | obj1 |   | (line_overlap)
                #   +------+ - - |      |   -
                #          - - - +------+
                #
                #          |<--->|
                #        (char_margin)
                halign = \
                    obj0.is_compatible(obj1) \
                    and obj0.is_voverlap(obj1) \
                    and min(obj0.height, obj1.height) * laparams.line_overlap \
                    < obj0.voverlap(obj1) \
                    and obj0.hdistance(obj1) \
                    < max(obj0.width, obj1.width) * laparams.char_margin_for_word

                # valign: obj0 and obj1 is vertically aligned.
                #
                #   +------+
                #   | obj0 |
                #   |      |
                #   +------+ - - -
                #     |    |     | (char_margin)
                #     +------+ - -
                #     | obj1 |
                #     |      |
                #     +------+
                #
                #     |<-->|
                #   (line_overlap)
                valign = \
                    laparams.detect_vertical \
                    and obj0.is_compatible(obj1) \
                    and obj0.is_hoverlap(obj1) \
                    and min(obj0.width, obj1.width) * laparams.line_overlap \
                    < obj0.hoverlap(obj1) \
                    and obj0.vdistance(obj1) \
                    < max(obj0.height, obj1.height) * laparams.char_margin_for_word

                if halign and isinstance(
                        word, LTTextWordHorizontal
                ) and obj1.get_text() != " " and obj1.get_text() != "\n":
                    word.add(obj1)
                elif word is not None:

                    yield word
                    word = None
                else:
                    if valign and not halign:

                        word = LTTextWordVertical(
                            laparams.char_margin_for_word)
                        if obj0.get_text() != " " and obj0.get_text() != "\n":
                            word.add(obj0)
                        if obj1.get_text() != " " and obj1.get_text() != "\n":
                            word.add(obj1)

                    elif halign and not valign:
                        word = LTTextWordHorizontal(
                            laparams.char_margin_for_word)
                        if obj0.get_text() != " " and obj0.get_text() != "\n":
                            word.add(obj0)
                        if obj1.get_text() != " " and obj1.get_text() != "\n":
                            word.add(obj1)
                    else:
                        word = LTTextWordHorizontal(
                            laparams.char_margin_for_word)
                        if obj1.get_text() != " " and obj1.get_text() != "\n":
                            word.add(obj0)
                        yield word
                        word = None
            obj0 = obj1
        if word is None:
            word = LTTextWordHorizontal(laparams.char_margin_for_word)
            word.add(obj0)

        yield word
        return

    def customY(self, word):
        return -word.bbox[1]

    def analyze(self, laparams):
        # textobjs is a list of LTChar objects, i.e.
        # it has all the individual characters in the page.
        (textobjs, otherobjs) = fsplit(lambda obj: isinstance(obj, LTChar),
                                       self)
        for obj in otherobjs:
            obj.analyze(laparams)
        if not textobjs:
            return
        textwords = list(self.group_textchars(laparams, textobjs))
        textlines = list(self.group_objects(laparams, textwords))
        (empties, textlines) = fsplit(lambda obj: obj.is_empty(), textlines)

        for obj in empties:
            obj.analyze(laparams)

        textboxes = list(self.group_textlines(laparams, textlines))
        for temp_box in textboxes:
            temp_words = list()
            for line in temp_box:
                if isinstance(line, LTTextLine):
                    for word in line:
                        if isinstance(word, LTTextWord):
                            temp_words.append(word)

            temp_words.sort(key=self.customY)
            test_string = ""
            for obj in temp_words:
                test_string += obj.get_text()
            if self.is_date(test_string):
                temp_box.type = "date"
            if self.is_header(test_string):
                temp_box.type = "header"
            if self.is_cl_bal(test_string):
                temp_box.form_field = "closing_balance"
            if self.is_account_number(test_string):
                temp_box.form_field = "account_number"
        if laparams.boxes_flow is None:
            for textbox in textboxes:
                textbox.analyze(laparams)

            def getkey(box):
                if isinstance(box, LTTextBoxVertical):
                    return (0, -box.x1, -box.y0)
                else:
                    return (1, -box.y0, box.x0)

            textboxes.sort(key=getkey)
        else:
            self.groups = self.group_textboxes(laparams, textboxes)
            assigner = IndexAssigner()
            for group in self.groups:
                group.analyze(laparams)
                assigner.run(group)
            textboxes.sort(key=lambda box: box.index)
        self._objs = textboxes + otherobjs + empties
        return


class LTFigure(LTLayoutContainer):
    """Represents an area used by PDF Form objects.

    PDF Forms can be used to present figures or pictures by embedding yet
    another PDF document within a page. Note that LTFigure objects can appear
    recursively.
    """

    def __init__(self, name, bbox, matrix):
        self.name = name
        self.matrix = matrix
        (x, y, w, h) = bbox
        bounds = ((x, y), (x + w, y), (x, y + h), (x + w, y + h))
        bbox = get_bound(apply_matrix_pt(matrix, (p, q)) for (p, q) in bounds)
        LTLayoutContainer.__init__(self, bbox)
        return

    def __repr__(self):
        return ('<%s(%s) %s matrix=%s>' %
                (self.__class__.__name__, self.name, bbox2str(
                    self.bbox), matrix2str(self.matrix)))

    def analyze(self, laparams):
        if not laparams.all_texts:
            return
        LTLayoutContainer.analyze(self, laparams)
        return


class LTPage(LTLayoutContainer):
    """Represents an entire page.

    May contain child objects like LTTextBox, LTFigure, LTImage, LTRect,
    LTCurve and LTLine.
    """

    def __init__(self, pageid, bbox, rotate=0):
        LTLayoutContainer.__init__(self, bbox)
        self.pageid = pageid
        self.rotate = rotate
        return

    def __repr__(self):
        return ('<%s(%r) %s rotate=%r>' %
                (self.__class__.__name__, self.pageid, bbox2str(
                    self.bbox), self.rotate))
