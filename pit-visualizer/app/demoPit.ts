const DEMO_PIT = `<?xml version="1.0" encoding="utf-8"?>
<Peach xmlns="http://peachfuzzer.com/2012/Peach"
       xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
       xsi:schemaLocation="http://peachfuzzer.com/2012/Peach /peach/peach.xsd">

  <Defaults>
    <Number signed="false" endian="big"/>
  </Defaults>

  <!-- ============================================================
       Common types
       ============================================================ -->

  <!-- MQTT UTF-8 String: 2-byte length prefix (big endian) + UTF-8 string -->
  <DataModel name="MQTT_String">
    <Number name="length" size="16">
      <Relation type="size" of="value"/>
    </Number>
    <String name="value" type="utf8"/>
  </DataModel>

  <!-- Binary Data: 2-byte length prefix + bytes -->
  <DataModel name="MQTT_BinaryData">
    <Number name="length" size="16">
      <Relation type="size" of="value"/>
    </Number>
    <Blob name="value"/>
  </DataModel>

  <!-- Topic Name is an MQTT UTF-8 String (with additional constraints not modeled here) -->
  <DataModel name="MQTT_TopicName" ref="MQTT_String"/>

  <!-- Fixed Header: message_type (4 bits) + flags (4 bits) + remaining_length (MqttVarInt) -->
  <DataModel name="mqtt_fixed_header_t">
    <Number name="message_type" size="4"/>
    <Number name="flags" size="4"/>
    <MqttVarInt name="remaining_length">
      <Relation type="size" of="msg_body"/>
    </MqttVarInt>
  </DataModel>

  <!-- Generic Properties container used by most MQTT 5 variable headers.
       Property Length is a Variable Byte Integer, followed by that many bytes. -->
  <DataModel name="mqtt_properties_t">
    <MqttVarInt name="property_length">
      <Relation type="size" of="properties"/>
    </MqttVarInt>
    <Blob name="properties"/>
  </DataModel>

  <!-- Packet Identifier -->
  <DataModel name="mqtt_packet_identifier_t">
    <Number name="packet_identifier" size="16"/>
  </DataModel>

  <!-- ============================================================
       CONNECT (1)
       ============================================================ -->

  <DataModel name="mqtt_connect_variable_header_t">
    <Block name="protocol_name" ref="MQTT_String"/>
    <Number name="protocol_level" size="8" value="5" token="false"/>
    <Number name="connect_flags" size="8"/>
    <Number name="keep_alive" size="16"/>
    <Block name="props" ref="mqtt_properties_t"/>
  </DataModel>

  <DataModel name="mqtt_connect_payload_t">
    <Block name="client_id" ref="MQTT_String"/>

    <!-- Will Flag (bit 2) -->
    <Optional name="will_optional" src="variable_header.connect_flags" expression="(value &amp; 0x04) != 0">
      <Block name="will_props" ref="mqtt_properties_t"/>
      <Block name="will_topic" ref="MQTT_String"/>
      <Block name="will_payload" ref="MQTT_BinaryData"/>
    </Optional>

    <!-- Username Flag (bit 7) -->
    <Optional name="username_optional" src="variable_header.connect_flags" expression="(value &amp; 0x80) != 0">
      <Block name="user_name" ref="MQTT_String"/>
    </Optional>

    <!-- Password Flag (bit 6) -->
    <Optional name="password_optional" src="variable_header.connect_flags" expression="(value &amp; 0x40) != 0">
      <Block name="password" ref="MQTT_BinaryData"/>
    </Optional>
  </DataModel>

  <DataModel name="mqtt_connect_packet_t">
    <Block name="fixed_header" ref="mqtt_fixed_header_t">
      <Number name="message_type" size="4" value="1" token="true"/>
      <Number name="flags" size="4" value="0" token="true"/>
    </Block>
    <Block name="msg_body">
      <Block name="variable_header" ref="mqtt_connect_variable_header_t"/>
      <Block name="payload" ref="mqtt_connect_payload_t"/>
    </Block>
  </DataModel>

  <!-- ============================================================
       CONNACK (2)
       ============================================================ -->

  <DataModel name="mqtt_connack_variable_header_t">
    <Number name="ack_flags" size="8"/>
    <Number name="reason_code" size="8"/>
    <Block name="props" ref="mqtt_properties_t"/>
  </DataModel>

  <DataModel name="mqtt_connack_packet_t">
    <Block name="fixed_header" ref="mqtt_fixed_header_t">
      <Number name="message_type" size="4" value="2" token="true"/>
      <Number name="flags" size="4" value="0" token="true"/>
    </Block>
    <Block name="msg_body">
      <Block name="variable_header" ref="mqtt_connack_variable_header_t"/>
    </Block>
  </DataModel>

  <!-- ============================================================
       PUBLISH (3)
       ============================================================ -->

  <DataModel name="mqtt_publish_variable_header_t">
    <Block name="topic_name" ref="MQTT_TopicName"/>

    <!-- Packet Identifier present if QoS != 0 (QoS bits are bits 1-2 of fixed header flags) -->
    <Optional name="packet_id_optional" src="fixed_header.flags" expression="((value &amp; 0x06) != 0)">
      <Number name="packet_identifier" size="16"/>
    </Optional>

    <Block name="props" ref="mqtt_properties_t"/>
  </DataModel>

  <!-- Payload is application data; in MQTT it consumes the remaining bytes. -->
  <DataModel name="mqtt_publish_payload_t">
    <Blob name="payload"/>
  </DataModel>

  <DataModel name="mqtt_publish_packet_t">
    <Block name="fixed_header" ref="mqtt_fixed_header_t">
      <Number name="message_type" size="4" value="3" token="true"/>
      <!-- flags are variable in PUBLISH (DUP/QoS/RETAIN) -->
      <Number name="flags" size="4"/>
    </Block>
    <Block name="msg_body">
      <Block name="variable_header" ref="mqtt_publish_variable_header_t"/>
      <Block name="payload" ref="mqtt_publish_payload_t"/>
    </Block>
  </DataModel>

  <!-- ============================================================
       PUBACK (4), PUBREC (5), PUBREL (6), PUBCOMP (7)
       MQTTv5 note: Reason Code and Properties are OPTIONAL depending on Remaining Length:
         - Remaining Length == 2 : only Packet Identifier
         - Remaining Length == 3 : Packet Identifier + Reason Code
         - Remaining Length >= 4 : Packet Identifier + Reason Code + Properties
       ============================================================ -->

  <DataModel name="mqtt_puback_variable_header_t">
    <Number name="packet_identifier" size="16"/>
    <Optional name="reason_code_optional" src="fixed_header.remaining_length" expression="value &gt;= 3">
      <Number name="reason_code" size="8"/>
    </Optional>
    <Optional name="props_optional" src="fixed_header.remaining_length" expression="value &gt;= 4">
      <Block name="props" ref="mqtt_properties_t"/>
    </Optional>
  </DataModel>

  <DataModel name="mqtt_pubrec_variable_header_t">
    <Number name="packet_identifier" size="16"/>
    <Optional name="reason_code_optional" src="fixed_header.remaining_length" expression="value &gt;= 3">
      <Number name="reason_code" size="8"/>
    </Optional>
    <Optional name="props_optional" src="fixed_header.remaining_length" expression="value &gt;= 4">
      <Block name="props" ref="mqtt_properties_t"/>
    </Optional>
  </DataModel>

  <DataModel name="mqtt_pubrel_variable_header_t">
    <Number name="packet_identifier" size="16"/>
    <Optional name="reason_code_optional" src="fixed_header.remaining_length" expression="value &gt;= 3">
      <Number name="reason_code" size="8"/>
    </Optional>
    <Optional name="props_optional" src="fixed_header.remaining_length" expression="value &gt;= 4">
      <Block name="props" ref="mqtt_properties_t"/>
    </Optional>
  </DataModel>

  <DataModel name="mqtt_pubcomp_variable_header_t">
    <Number name="packet_identifier" size="16"/>
    <Optional name="reason_code_optional" src="fixed_header.remaining_length" expression="value &gt;= 3">
      <Number name="reason_code" size="8"/>
    </Optional>
    <Optional name="props_optional" src="fixed_header.remaining_length" expression="value &gt;= 4">
      <Block name="props" ref="mqtt_properties_t"/>
    </Optional>
  </DataModel>

  <DataModel name="mqtt_puback_packet_t">
    <Block name="fixed_header" ref="mqtt_fixed_header_t">
      <Number name="message_type" size="4" value="4" token="true"/>
      <Number name="flags" size="4" value="0" token="true"/>
    </Block>
    <Block name="msg_body">
      <Block name="variable_header" ref="mqtt_puback_variable_header_t"/>
    </Block>
  </DataModel>

  <DataModel name="mqtt_pubrec_packet_t">
    <Block name="fixed_header" ref="mqtt_fixed_header_t">
      <Number name="message_type" size="4" value="5" token="true"/>
      <Number name="flags" size="4" value="0" token="true"/>
    </Block>
    <Block name="msg_body">
      <Block name="variable_header" ref="mqtt_pubrec_variable_header_t"/>
    </Block>
  </DataModel>

  <DataModel name="mqtt_pubrel_packet_t">
    <Block name="fixed_header" ref="mqtt_fixed_header_t">
      <Number name="message_type" size="4" value="6" token="true"/>
      <!-- PUBREL flags are 0b0010 -->
      <Number name="flags" size="4" value="2" token="true"/>
    </Block>
    <Block name="msg_body">
      <Block name="variable_header" ref="mqtt_pubrel_variable_header_t"/>
    </Block>
  </DataModel>

  <DataModel name="mqtt_pubcomp_packet_t">
    <Block name="fixed_header" ref="mqtt_fixed_header_t">
      <Number name="message_type" size="4" value="7" token="true"/>
      <Number name="flags" size="4" value="0" token="true"/>
    </Block>
    <Block name="msg_body">
      <Block name="variable_header" ref="mqtt_pubcomp_variable_header_t"/>
    </Block>
  </DataModel>

  <!-- ============================================================
       SUBSCRIBE (8) / SUBACK (9)
       ============================================================ -->

  <DataModel name="mqtt_subscribe_topic_filter_t">
    <Block name="topic_filter" ref="MQTT_String"/>
    <Number name="subscription_options" size="8"/>
  </DataModel>

  <DataModel name="mqtt_subscribe_payload_t">
    <Block name="topic_filters" minOccurs="1" maxOccurs="100">
      <Block ref="mqtt_subscribe_topic_filter_t"/>
    </Block>
  </DataModel>

  <DataModel name="mqtt_subscribe_variable_header_t">
    <Number name="packet_identifier" size="16"/>
    <Block name="props" ref="mqtt_properties_t"/>
  </DataModel>

  <DataModel name="mqtt_subscribe_packet_t">
    <Block name="fixed_header" ref="mqtt_fixed_header_t">
      <Number name="message_type" size="4" value="8" token="true"/>
      <!-- SUBSCRIBE flags are 0b0010 -->
      <Number name="flags" size="4" value="2" token="true"/>
    </Block>
    <Block name="msg_body">
      <Block name="variable_header" ref="mqtt_subscribe_variable_header_t"/>
      <Block name="payload" ref="mqtt_subscribe_payload_t"/>
    </Block>
  </DataModel>

  <DataModel name="mqtt_suback_payload_t">
    <Block name="reason_codes" minOccurs="1" maxOccurs="100">
      <Number name="reason_code" size="8"/>
    </Block>
  </DataModel>

  <DataModel name="mqtt_suback_variable_header_t">
    <Number name="packet_identifier" size="16"/>
    <Block name="props" ref="mqtt_properties_t"/>
  </DataModel>

  <DataModel name="mqtt_suback_packet_t">
    <Block name="fixed_header" ref="mqtt_fixed_header_t">
      <Number name="message_type" size="4" value="9" token="true"/>
      <Number name="flags" size="4" value="0" token="true"/>
    </Block>
    <Block name="msg_body">
      <Block name="variable_header" ref="mqtt_suback_variable_header_t"/>
      <Block name="payload" ref="mqtt_suback_payload_t"/>
    </Block>
  </DataModel>

  <!-- ============================================================
       UNSUBSCRIBE (10) / UNSUBACK (11)
       ============================================================ -->

  <DataModel name="mqtt_unsubscribe_payload_t">
    <Block name="topic_filters" minOccurs="1" maxOccurs="100">
      <Block name="topic_filter" ref="MQTT_String"/>
    </Block>
  </DataModel>

  <DataModel name="mqtt_unsubscribe_variable_header_t">
    <Number name="packet_identifier" size="16"/>
    <Block name="props" ref="mqtt_properties_t"/>
  </DataModel>

  <DataModel name="mqtt_unsubscribe_packet_t">
    <Block name="fixed_header" ref="mqtt_fixed_header_t">
      <Number name="message_type" size="4" value="10" token="true"/>
      <!-- UNSUBSCRIBE flags are 0b0010 -->
      <Number name="flags" size="4" value="2" token="true"/>
    </Block>
    <Block name="msg_body">
      <Block name="variable_header" ref="mqtt_unsubscribe_variable_header_t"/>
      <Block name="payload" ref="mqtt_unsubscribe_payload_t"/>
    </Block>
  </DataModel>

  <DataModel name="mqtt_unsuback_variable_header_t">
    <Number name="packet_identifier" size="16"/>
    <Block name="props" ref="mqtt_properties_t"/>
  </DataModel>

  <DataModel name="mqtt_unsuback_payload_t">
    <Block name="reason_codes" minOccurs="1" maxOccurs="100">
      <Number name="reason_code" size="8"/>
    </Block>
  </DataModel>

  <DataModel name="mqtt_unsuback_packet_t">
    <Block name="fixed_header" ref="mqtt_fixed_header_t">
      <Number name="message_type" size="4" value="11" token="true"/>
      <Number name="flags" size="4" value="0" token="true"/>
    </Block>
    <Block name="msg_body">
      <Block name="variable_header" ref="mqtt_unsuback_variable_header_t"/>
      <Block name="payload" ref="mqtt_unsuback_payload_t"/>
    </Block>
  </DataModel>

  <!-- ============================================================
       PINGREQ (12) / PINGRESP (13)
       ============================================================ -->

  <DataModel name="mqtt_pingreq_packet_t">
    <Block name="fixed_header" ref="mqtt_fixed_header_t">
      <Number name="message_type" size="4" value="12" token="true"/>
      <Number name="flags" size="4" value="0" token="true"/>
    </Block>
    <Block name="msg_body"/>
  </DataModel>

  <DataModel name="mqtt_pingresp_packet_t">
    <Block name="fixed_header" ref="mqtt_fixed_header_t">
      <Number name="message_type" size="4" value="13" token="true"/>
      <Number name="flags" size="4" value="0" token="true"/>
    </Block>
    <Block name="msg_body"/>
  </DataModel>

  <!-- ============================================================
       DISCONNECT (14)
       MQTTv5 note: Variable Header can be omitted (Remaining Length == 0).
         - Remaining Length == 0 : no reason code, no properties
         - Remaining Length == 1 : reason code only
         - Remaining Length >= 2 : reason code + properties
       ============================================================ -->

  <DataModel name="mqtt_disconnect_variable_header_t">
    <Optional name="reason_code_optional" src="fixed_header.remaining_length" expression="value &gt;= 1">
      <Number name="reason_code" size="8"/>
    </Optional>
    <Optional name="props_optional" src="fixed_header.remaining_length" expression="value &gt;= 2">
      <Block name="props" ref="mqtt_properties_t"/>
    </Optional>
  </DataModel>

  <DataModel name="mqtt_disconnect_packet_t">
    <Block name="fixed_header" ref="mqtt_fixed_header_t">
      <Number name="message_type" size="4" value="14" token="true"/>
      <Number name="flags" size="4" value="0" token="true"/>
    </Block>
    <Block name="msg_body">
      <Block name="variable_header" ref="mqtt_disconnect_variable_header_t"/>
    </Block>
  </DataModel>

  <!-- ============================================================
       AUTH (15)
       ============================================================ -->

  <DataModel name="mqtt_auth_variable_header_t">
    <Number name="reason_code" size="8"/>
    <Block name="props" ref="mqtt_properties_t"/>
  </DataModel>

  <DataModel name="mqtt_auth_packet_t">
    <Block name="fixed_header" ref="mqtt_fixed_header_t">
      <Number name="message_type" size="4" value="15" token="true"/>
      <Number name="flags" size="4" value="0" token="true"/>
    </Block>
    <Block name="msg_body">
      <Block name="variable_header" ref="mqtt_auth_variable_header_t"/>
    </Block>
  </DataModel>

  <!-- ============================================================
       Reserved (0) (not valid on the wire, but modeled for completeness)
       ============================================================ -->

  <DataModel name="mqtt_reserved_packet_t">
    <Block name="fixed_header" ref="mqtt_fixed_header_t">
      <Number name="message_type" size="4" value="0" token="true"/>
      <Number name="flags" size="4"/>
    </Block>
    <Block name="msg_body">
      <Blob name="data"/>
    </Block>
  </DataModel>

  <!-- ============================================================
       Union of all packet types
       ============================================================ -->

  <DataModel name="mqtt_packet_t">
    <Choice name="packet_union">
      <Block name="reserved" ref="mqtt_reserved_packet_t"/>
      <Block name="connect" ref="mqtt_connect_packet_t"/>
      <Block name="connack" ref="mqtt_connack_packet_t"/>
      <Block name="publish" ref="mqtt_publish_packet_t"/>
      <Block name="puback" ref="mqtt_puback_packet_t"/>
      <Block name="pubrec" ref="mqtt_pubrec_packet_t"/>
      <Block name="pubrel" ref="mqtt_pubrel_packet_t"/>
      <Block name="pubcomp" ref="mqtt_pubcomp_packet_t"/>
      <Block name="subscribe" ref="mqtt_subscribe_packet_t"/>
      <Block name="suback" ref="mqtt_suback_packet_t"/>
      <Block name="unsubscribe" ref="mqtt_unsubscribe_packet_t"/>
      <Block name="unsuback" ref="mqtt_unsuback_packet_t"/>
      <Block name="pingreq" ref="mqtt_pingreq_packet_t"/>
      <Block name="pingresp" ref="mqtt_pingresp_packet_t"/>
      <Block name="disconnect" ref="mqtt_disconnect_packet_t"/>
      <Block name="auth" ref="mqtt_auth_packet_t"/>
    </Choice>
  </DataModel>

  <!-- mqtt_packet_t[] - Array of MQTT packets -->
  <DataModel name="mqtt_packet_array">
    <Block name="packets" minOccurs="1" maxOccurs="100">
      <Block ref="mqtt_packet_t"/>
    </Block>
  </DataModel>

</Peach>`;

export default DEMO_PIT;

