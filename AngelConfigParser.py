import struct
import zlib
import sys
from pathlib import Path
from io import BytesIO
from datetime import datetime
import xml.dom.minidom


class AMF3Decoder:
    """
    AMF3 格式解码器

    AMF3 数据类型标记：
    0x00 - Undefined
    0x01 - Null
    0x02 - False
    0x03 - True
    0x04 - Integer
    0x05 - Double
    0x06 - String
    0x07 - XML Document
    0x08 - Date
    0x09 - Array
    0x0A - Object
    0x0B - XML
    0x0C - ByteArray
    """

    # AMF3 类型标记
    UNDEFINED_TYPE = 0x00
    NULL_TYPE = 0x01
    FALSE_TYPE = 0x02
    TRUE_TYPE = 0x03
    INTEGER_TYPE = 0x04
    DOUBLE_TYPE = 0x05
    STRING_TYPE = 0x06
    XML_DOC_TYPE = 0x07
    DATE_TYPE = 0x08
    ARRAY_TYPE = 0x09
    OBJECT_TYPE = 0x0A
    XML_TYPE = 0x0B
    BYTEARRAY_TYPE = 0x0C

    def __init__(self, data):
        if isinstance(data, bytes):
            self.stream = BytesIO(data)
        else:
            self.stream = data

        # AMF3引用表
        self.string_table = []
        self.object_table = []
        self.trait_table = []

    def read_byte(self):
        """读取1字节"""
        b = self.stream.read(1)
        if len(b) == 0:
            raise EOFError("读取到文件末尾")
        return b[0]

    def read_bytes(self, n):
        """读取n字节"""
        data = self.stream.read(n)
        if len(data) < n:
            raise EOFError(f"期望读取 {n} 字节，实际只有 {len(data)} 字节")
        return data

    def read_double(self):
        """读取8字节双精度浮点数 (big-endian)"""
        return struct.unpack(">d", self.read_bytes(8))[0]

    def read_u29(self):
        """读取AMF3的U29可变长度整数"""
        result = 0
        for i in range(4):
            byte = self.read_byte()
            if i < 3:
                result = (result << 7) | (byte & 0x7F)
                if (byte & 0x80) == 0:
                    return result
            else:
                result = (result << 8) | byte
        return result

    def read_element(self):
        """读取一个AMF3元素"""
        type_marker = self.read_byte()

        if type_marker == self.UNDEFINED_TYPE:
            return None
        elif type_marker == self.NULL_TYPE:
            return None
        elif type_marker == self.FALSE_TYPE:
            return False
        elif type_marker == self.TRUE_TYPE:
            return True
        elif type_marker == self.INTEGER_TYPE:
            return self.read_integer()
        elif type_marker == self.DOUBLE_TYPE:
            return self.read_double()
        elif type_marker == self.STRING_TYPE:
            return self.read_string()
        elif type_marker == self.XML_DOC_TYPE:
            return self.read_xml_doc()
        elif type_marker == self.DATE_TYPE:
            return self.read_date()
        elif type_marker == self.ARRAY_TYPE:
            return self.read_array()
        elif type_marker == self.OBJECT_TYPE:
            return self.read_object()
        elif type_marker == self.XML_TYPE:
            return self.read_xml()
        elif type_marker == self.BYTEARRAY_TYPE:
            return self.read_bytearray()
        else:
            raise ValueError(f"未知的AMF3类型标记: 0x{type_marker:02X}")

    def read_integer(self):
        """读取AMF3整数"""
        value = self.read_u29()
        # 转换为有符号整数
        if value & 0x10000000:
            value -= 0x20000000
        return value

    def read_string(self):
        """读取AMF3字符串（带引用表）"""
        u29 = self.read_u29()

        if (u29 & 1) == 0:
            # 这是一个引用
            ref = u29 >> 1
            if ref >= len(self.string_table):
                raise ValueError(f"字符串引用越界: {ref}")
            return self.string_table[ref]

        # 读取新字符串
        length = u29 >> 1
        if length == 0:
            return ""

        string = self.read_bytes(length).decode("utf-8", errors="replace")
        # 非空字符串才加入引用表
        if string:
            self.string_table.append(string)
        return string

    def read_date(self):
        """读取AMF3日期"""
        u29 = self.read_u29()

        if (u29 & 1) == 0:
            # 这是一个引用
            ref = u29 >> 1
            if ref >= len(self.object_table):
                raise ValueError(f"对象引用越界: {ref}")
            return self.object_table[ref]

        # 读取新日期
        timestamp = self.read_double()  # 毫秒时间戳
        date_obj = datetime.fromtimestamp(timestamp / 1000.0)
        self.object_table.append(date_obj)
        return date_obj

    def read_array(self):
        """读取AMF3数组"""
        u29 = self.read_u29()

        if (u29 & 1) == 0:
            # 这是一个引用
            ref = u29 >> 1
            if ref >= len(self.object_table):
                raise ValueError(f"对象引用越界: {ref}")
            return self.object_table[ref]

        # 读取数组大小
        size = u29 >> 1

        # 先创建数组并加入引用表
        arr = []
        self.object_table.append(arr)

        # 读取关联部分（键值对）
        assoc = {}
        while True:
            key = self.read_string()
            if key == "":
                break
            value = self.read_element()
            assoc[key] = value

        # 如果有关联属性，转换为字典
        if len(assoc) > 0:
            arr_dict = assoc
            # 读取密集部分
            for i in range(size):
                arr_dict[str(i)] = self.read_element()
            # 更新引用表中的对象
            self.object_table[-1] = arr_dict
            return arr_dict
        else:
            # 纯数组
            for i in range(size):
                arr.append(self.read_element())
            return arr

    def read_object(self):
        """读取AMF3对象"""
        u29 = self.read_u29()

        if (u29 & 1) == 0:
            # 这是一个引用
            ref = u29 >> 1
            if ref >= len(self.object_table):
                raise ValueError(f"对象引用越界: {ref}")
            return self.object_table[ref]

        # 读取trait信息
        if (u29 & 2) == 0:
            # trait引用
            trait_ref = u29 >> 2
            if trait_ref >= len(self.trait_table):
                raise ValueError(f"trait引用越界: {trait_ref}")
            trait = self.trait_table[trait_ref]
        else:
            # 新trait
            trait = self._read_trait(u29)
            self.trait_table.append(trait)

        # 创建对象并加入引用表
        obj = {}
        self.object_table.append(obj)

        if trait["class_name"]:
            obj["__class__"] = trait["class_name"]

        # 处理可外部化对象
        if trait["externalizable"]:
            # 可外部化对象的数据由对象自己处理
            obj["__data__"] = self.read_element()
            return obj

        # 读取密封属性
        for prop_name in trait["properties"]:
            obj[prop_name] = self.read_element()

        # 读取动态属性
        if trait["dynamic"]:
            while True:
                key = self.read_string()
                if key == "":
                    break
                value = self.read_element()
                obj[key] = value

        return obj

    def _read_trait(self, u29):
        """读取trait定义"""
        trait = {}
        trait["externalizable"] = (u29 & 4) != 0
        trait["dynamic"] = (u29 & 8) != 0

        prop_count = u29 >> 4
        trait["class_name"] = self.read_string()
        trait["properties"] = []

        for i in range(prop_count):
            prop_name = self.read_string()
            trait["properties"].append(prop_name)

        return trait

    def read_xml(self):
        """读取AMF3 XML"""
        u29 = self.read_u29()

        if (u29 & 1) == 0:
            # 这是一个引用
            ref = u29 >> 1
            if ref >= len(self.object_table):
                raise ValueError(f"对象引用越界: {ref}")
            return self.object_table[ref]

        # 读取XML字符串
        length = u29 >> 1
        xml_str = self.read_bytes(length).decode("utf-8", errors="replace")
        self.object_table.append(xml_str)
        return xml_str

    def read_xml_doc(self):
        """读取AMF3 XML Document"""
        return self.read_xml()

    def read_bytearray(self):
        """读取AMF3 ByteArray"""
        u29 = self.read_u29()

        if (u29 & 1) == 0:
            # 这是一个引用
            ref = u29 >> 1
            if ref >= len(self.object_table):
                raise ValueError(f"对象引用越界: {ref}")
            return self.object_table[ref]

        # 读取字节数组
        length = u29 >> 1
        byte_array = self.read_bytes(length)
        self.object_table.append(byte_array)
        return byte_array


class AngelConfigParser:
    """Angel.config 解析器 — 支持文件路径或 bytes"""

    def __init__(self, source):
        """
        source: 文件路径 (str/Path) 或 字节数据 (bytes)
        """
        self.source = source
        self.flag = None
        self.data_size = None
        self.config_count = None
        self.configs = []

    def parse(self):
        """解析配置"""
        try:
            if isinstance(self.source, bytes):
                data = self.source
                label = f"内存 ({len(data)} 字节)"
            else:
                with open(self.source, "rb") as f:
                    data = f.read()
                label = self.source

            print(f"[+] 读取: {label}")
            print(f"[+] 文件大小: {len(data)} 字节\n")

            # 解析头部
            offset = 0

            # 1 byte: 标志位
            self.flag = struct.unpack("B", data[offset : offset + 1])[0]
            offset += 1
            print(f"[*] 标志位: 0x{self.flag:02X}")

            # 4 bytes: 数据大小
            self.data_size = struct.unpack(">I", data[offset : offset + 4])[0]
            offset += 4
            print(f"[*] 数据大小: {self.data_size} 字节")

            # 2 bytes: 配置数量
            self.config_count = struct.unpack(">H", data[offset : offset + 2])[0]
            offset += 2
            print(f"[*] 配置数量: {self.config_count}")

            # 验证数据大小
            compressed_data_size = len(data) - offset
            if self.data_size != compressed_data_size:
                print(
                    f"[!] 警告: 数据大小不匹配 (期望:{self.data_size}, 实际:{compressed_data_size})"
                )

            # 解压数据
            compressed_data = data[offset:]
            print(f"\n[*] 压缩数据大小: {len(compressed_data)} 字节")

            try:
                decompressed_data = zlib.decompress(compressed_data)
                print(f"[*] 解压后大小: {len(decompressed_data)} 字节")
            except zlib.error as e:
                print(f"[!] 解压失败: {e}")
                return False

            # 解析AMF对象
            print(f"\n[*] 开始解析 {self.config_count} 个配置对象...\n")
            self._parse_amf_objects(decompressed_data)

            return True

        except FileNotFoundError:
            print(f"[!] 错误: 文件不存在 - {self.file_path}")
            return False
        except Exception as e:
            print(f"[!] 解析错误: {e}")
            import traceback

            traceback.print_exc()
            return False

    def _parse_amf_objects(self, data):
        """解析AMF对象"""
        # Flash的ByteArray.writeObject()使用AMF3格式
        decoder = AMF3Decoder(data)

        for i in range(self.config_count):
            try:
                # 使用自定义AMF3解码器
                obj = decoder.read_element()

                print(f"[{i+1}/{self.config_count}] 配置对象:")
                print(f"  原始类型: {type(obj).__name__}")

                # 处理不同类型的对象
                if isinstance(obj, str):
                    # 直接是字符串（可能是XML）
                    xml_str = obj
                    data_type = "string"
                elif isinstance(obj, dict):
                    # 对象或类型化对象
                    xml_str = self._dict_to_xml(obj)
                    data_type = "object"
                elif isinstance(obj, list):
                    # 数组
                    xml_str = self._list_to_xml(obj)
                    data_type = "array"
                elif isinstance(obj, bytes):
                    # 字节数组，尝试转换为字符串
                    try:
                        xml_str = obj.decode("utf-8", errors="replace")
                        data_type = "bytes"
                    except:
                        xml_str = f"<Binary length='{len(obj)}' />"
                        data_type = "binary"
                elif isinstance(obj, (int, float, bool)):
                    xml_str = f"<Value>{obj}</Value>"
                    data_type = type(obj).__name__
                elif isinstance(obj, datetime):
                    xml_str = f"<Date>{obj.isoformat()}</Date>"
                    data_type = "datetime"
                elif obj is None:
                    xml_str = "<Null />"
                    data_type = "null"
                else:
                    # 其他类型，转换为字符串
                    xml_str = str(obj)
                    data_type = type(obj).__name__

                # 尝试提取name属性
                config_name = self._extract_name(xml_str)

                # 尝试格式化XML
                try:
                    dom = xml.dom.minidom.parseString(xml_str)
                    xml_str = dom.toprettyxml(indent="  ", encoding="utf-8").decode(
                        "utf-8"
                    )
                    # 移除XML声明和多余空行
                    lines = [line for line in xml_str.split("\n") if line.strip()]
                    if lines and lines[0].startswith("<?xml"):
                        lines = lines[1:]
                    xml_str = "\n".join(lines)
                except:
                    pass  # 格式化失败就使用原始字符串

                self.configs.append(
                    {
                        "index": i,
                        "name": config_name,
                        "data": xml_str,
                        "type": data_type,
                    }
                )

                print(f"  名称: {config_name}")
                print(f"  数据类型: {data_type}")
                print(f"  大小: {len(xml_str)} 字符")
                print()

            except EOFError:
                print(f"[!] 解析第 {i+1} 个对象时到达数据末尾")
                break
            except Exception as e:
                print(f"[!] 解析第 {i+1} 个对象失败: {e}")
                import traceback

                traceback.print_exc()
                break

    def _dict_to_xml(self, obj, indent=0):
        """将字典转换为XML字符串（支持嵌套）"""
        if "__class__" in obj:
            root_name = obj["__class__"]
        else:
            root_name = "Object"

        prefix = "  " * indent
        lines = [f"{prefix}<{root_name}>"]

        for key, value in obj.items():
            if key == "__class__":
                continue

            # 处理不同类型的值
            if isinstance(value, dict):
                nested = self._dict_to_xml(value, indent + 1)
                lines.append(f"{prefix}  <{key}>")
                lines.append(nested)
                lines.append(f"{prefix}  </{key}>")
            elif isinstance(value, list):
                nested = self._list_to_xml(value, indent + 1)
                lines.append(f"{prefix}  <{key}>")
                lines.append(nested)
                lines.append(f"{prefix}  </{key}>")
            elif isinstance(value, str):
                # 转义XML特殊字符
                escaped = self._escape_xml(value)
                lines.append(f"{prefix}  <{key}>{escaped}</{key}>")
            elif isinstance(value, (int, float, bool)):
                lines.append(f"{prefix}  <{key}>{value}</{key}>")
            elif isinstance(value, datetime):
                lines.append(f"{prefix}  <{key}>{value.isoformat()}</{key}>")
            elif value is None:
                lines.append(f"{prefix}  <{key} />")
            else:
                lines.append(f"{prefix}  <{key}>{str(value)}</{key}>")

        lines.append(f"{prefix}</{root_name}>")
        return "\n".join(lines)

    def _list_to_xml(self, arr, indent=0):
        """将列表转换为XML字符串"""
        prefix = "  " * indent
        lines = [f'{prefix}<Array length="{len(arr)}">']

        for i, value in enumerate(arr):
            if isinstance(value, dict):
                nested = self._dict_to_xml(value, indent + 1)
                lines.append(f'{prefix}  <Item index="{i}">')
                lines.append(nested)
                lines.append(f"{prefix}  </Item>")
            elif isinstance(value, list):
                nested = self._list_to_xml(value, indent + 1)
                lines.append(f'{prefix}  <Item index="{i}">')
                lines.append(nested)
                lines.append(f"{prefix}  </Item>")
            elif isinstance(value, str):
                escaped = self._escape_xml(value)
                lines.append(f'{prefix}  <Item index="{i}">{escaped}</Item>')
            else:
                lines.append(f'{prefix}  <Item index="{i}">{value}</Item>')

        lines.append(f"{prefix}</Array>")
        return "\n".join(lines)

    def _escape_xml(self, text):
        """转义XML特殊字符"""
        if not isinstance(text, str):
            return text
        text = text.replace("&", "&amp;")
        text = text.replace("<", "&lt;")
        text = text.replace(">", "&gt;")
        text = text.replace('"', "&quot;")
        text = text.replace("'", "&apos;")
        return text

    def _extract_name(self, xml_str):
        """从XML中提取name属性"""
        import re

        # 尝试匹配 <任意标签 name="xxx">
        match = re.search(r'<\w+[^>]*\sname=["\']([^"\']+)["\']', xml_str)
        if match:
            return match.group(1)
        # 尝试匹配根元素标签名
        match = re.search(r"<(\w+)", xml_str)
        if match:
            return match.group(1)
        return "未知"

    def export_configs(self, output_dir):
        """导出所有配置到文件"""
        output_path = Path(output_dir)
        output_path.mkdir(parents=True, exist_ok=True)

        for config in self.configs:
            filename = f"{config['index']:02d}_{config['name']}.xml"
            file_path = output_path / filename

            with open(file_path, "w", encoding="utf-8") as f:
                f.write(config["data"])
