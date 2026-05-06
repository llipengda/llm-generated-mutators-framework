using System;
using System.Text;
using System.Linq;
using NUnit.Framework;
using Peach.Core;
using Peach.Core.Dom;
using Peach.LLM.Core;
using Peach.LLM.Validations.Common;
using Peach.LLM.Generated.Fixups.MQTT;
using Encoding = System.Text.Encoding;
using Peach.LLM.Core.Dom;
using static Peach.LLM.Validations.Common.DataElementMaker;
using Array = System.Array;
using String = System.String;
using PArray = Peach.Core.Dom.Array;
using PString = Peach.Core.Dom.String;
using NLog;

namespace Peach.LLM.Generated.Validations.Fixer.MQTT
{
    public partial class MQTTFixerTest
    {
        // [MQTT-4.8.2-1] A Shared Subscription's Topic Filter MUST start with $share/ and MUST contain a ShareName that is at least one character long.
        [FixerTest("MQTT-4.8.2-1")]
        public static void Test_FixMQTT_4_8_2_1()
        {
            var logger = NLog.LogManager.GetCurrentClassLogger();
            var packetArray =
                Make<DataModel>("mqtt_packet_array",
                    Make<Block>("packets",
                        Make<Block>("mqtt_packet_t",
                            Make<Choice>("packet_union",
                                Make<DataModel>("subscribe",
                                    Make<DataModel>("fixed_header",
                                        Make<Number>("message_type", 8),
                                        Make<Number>("flags", 2),
                                        Make<MqttVarInt>("remaining_length", 0)
                                    ),
                                    Make<Block>("msg_body",
                                        Make<Number>("packet_identifier", 1),
                                        Make<DataModel>("props",
                                            Make<MqttVarInt>("property_length", 0),
                                            Make<Blob>("properties", new byte[0])
                                        ),
                                        Make<DataModel>("payload",
                                            Make<PArray>("topic_filters",
                                                Make<DataModel>("topic_filters_0",
                                                    Make<DataModel>("topic_filter",
                                                        Make<Number>("length", 0),
                                                        Make<PString>("value", "$share//topic")
                                                    ),
                                                    Make<Number>("subscription_options", 0)
                                                )
                                            )
                                        )
                                    )
                                )
                            )
                        )
                    )
                );


            MQTTFixers.FixMQTT_4_8_2_1(packetArray.find("subscribe"));

            var fixedTopicFilterValue = packetArray.find("subscribe")
                .find("msg_body")
                .find("payload")
                .find("topic_filters_0")
                .find("topic_filter")
                .find("value");
            var fixedString = Encoding.UTF8.GetString(fixedTopicFilterValue.Bytes());

            Assert.IsNotNull(fixedTopicFilterValue, "Fixed topic filter value should not be null.");
            Assert.IsTrue(fixedString.StartsWith("$share/"), "Fixed topic filter should start with '$share/', but got: " + fixedString);
            Assert.Greater(fixedString.Length, "$share/".Length, "Fixed topic filter should have more characters after '$share/', but got: " + fixedString);
            Assert.IsFalse(fixedString.StartsWith("$share//"), "Fixed topic filter should not start with '$share//', but got: " + fixedString);
        }
    }
}
