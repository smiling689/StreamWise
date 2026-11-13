-- Wireshark dissector for the StreamWise segment protocol (see src/streamwise/common/protocol.py).
--
-- Install: copy to ~/.local/lib/wireshark/plugins/ (Linux) or %APPDATA%\Wireshark\plugins\
-- (Windows), or run:  tshark -X lua_script:tools/wireshark/streamwise.lua -r capture.pcapng
-- The TCP port defaults to 9000 and can be changed under Preferences > Protocols > StreamWise.

local streamwise = Proto("streamwise", "StreamWise Segment Protocol")

local HEADER_LEN = 16

local msg_types = {
    [1] = "HELLO", [2] = "WELCOME", [3] = "GET_MANIFEST", [4] = "MANIFEST",
    [5] = "GET_SEGMENT", [6] = "SEGMENT", [7] = "ERROR", [8] = "BYE",
}
local text_types = { [1] = true, [2] = true, [3] = true, [4] = true, [5] = true, [7] = true }

local f_magic   = ProtoField.string("streamwise.magic", "Magic")
local f_version = ProtoField.uint8("streamwise.version", "Version")
local f_type    = ProtoField.uint8("streamwise.type", "Type", base.DEC, msg_types)
local f_reqid   = ProtoField.uint32("streamwise.request_id", "Request ID")
local f_len     = ProtoField.uint32("streamwise.payload_length", "Payload Length")
local f_crc     = ProtoField.uint32("streamwise.crc32", "Payload CRC-32", base.HEX)
local f_text    = ProtoField.string("streamwise.text", "Payload")
local f_data    = ProtoField.bytes("streamwise.data", "Segment Data")
streamwise.fields = { f_magic, f_version, f_type, f_reqid, f_len, f_crc, f_text, f_data }

streamwise.prefs.port = Pref.uint("TCP port", 9000, "TCP port of the StreamWise server")

local function pdu_length(tvb, pinfo, offset)
    return HEADER_LEN + tvb(offset + 8, 4):uint()
end

local function dissect_pdu(tvb, pinfo, tree)
    local mtype = tvb(3, 1):uint()
    local reqid = tvb(4, 4):uint()
    local plen = tvb(8, 4):uint()
    local name = msg_types[mtype] or ("UNKNOWN(" .. mtype .. ")")

    pinfo.cols.protocol = "StreamWise"
    local subtree = tree:add(streamwise, tvb(), "StreamWise " .. name)
    subtree:add(f_magic, tvb(0, 2))
    subtree:add(f_version, tvb(2, 1))
    subtree:add(f_type, tvb(3, 1))
    subtree:add(f_reqid, tvb(4, 4))
    subtree:add(f_len, tvb(8, 4))
    subtree:add(f_crc, tvb(12, 4))

    local summary = name .. " id=" .. reqid
    if plen > 0 then
        local payload = tvb(HEADER_LEN, plen)
        if text_types[mtype] then
            subtree:add(f_text, payload)
            if mtype == 3 or mtype == 5 then
                summary = summary .. " " .. payload:string()
            end
        else
            subtree:add(f_data, payload)
            summary = summary .. string.format(" (%d bytes)", plen)
        end
    end
    pinfo.cols.info:append(" " .. summary)
    return tvb:len()
end

function streamwise.dissector(tvb, pinfo, tree)
    if tvb:len() >= 2 and tvb(0, 2):string() ~= "SW" then
        return 0
    end
    pinfo.cols.info:clear()
    dissect_tcp_pdus(tvb, tree, HEADER_LEN, pdu_length, dissect_pdu, true)
    return tvb:len()
end

local registered_port = nil
function streamwise.init()
    local tcp_table = DissectorTable.get("tcp.port")
    if registered_port then
        tcp_table:remove(registered_port, streamwise)
    end
    registered_port = streamwise.prefs.port
    tcp_table:add(registered_port, streamwise)
end
