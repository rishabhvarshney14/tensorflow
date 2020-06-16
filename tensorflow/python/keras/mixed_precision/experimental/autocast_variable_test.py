# Copyright 2019 The TensorFlow Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
"""Tests for AutoCastVariable."""
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import contextlib
import os

from absl.testing import parameterized
import numpy as np

from tensorflow.python import tf2
from tensorflow.python.distribute import combinations
from tensorflow.python.distribute import distribution_strategy_context as ds_context
from tensorflow.python.distribute import mirrored_strategy
from tensorflow.python.distribute import strategy_combinations
from tensorflow.python.eager import context
from tensorflow.python.eager import def_function
from tensorflow.python.framework import constant_op
from tensorflow.python.framework import dtypes
from tensorflow.python.framework import indexed_slices
from tensorflow.python.framework import ops
from tensorflow.python.keras.mixed_precision.experimental import autocast_variable
from tensorflow.python.keras.optimizer_v2 import gradient_descent as gradient_descent_v2
from tensorflow.python.ops import array_ops
from tensorflow.python.ops import state_ops
from tensorflow.python.ops import variables
from tensorflow.python.platform import test
from tensorflow.python.training import gradient_descent as gradient_descent_v1
from tensorflow.python.training.tracking import util as trackable_utils

class DummyStrategy(object):
  @contextlib.contextmanager
  def scope(self):
    yield

maybe_distribute = combinations.combine(
    distribution=[
        combinations.NamedDistribution(
            "Dummy", lambda: DummyStrategy(), required_gpus=None),
        strategy_combinations.mirrored_strategy_with_cpu_1_and_2
    ])

def get_var(val, dtype, name=None):
  return variables.VariableV1(val, use_resource=True, dtype=dtype, name=name)


@combinations.generate(combinations.combine(mode=['graph', 'eager']))
class AutoCastVariableTest(test.TestCase, parameterized.TestCase):
  def setUp(self):
    strategy_combinations.set_virtual_cpus_to_at_least(3)
    super(AutoCastVariableTest, self).setUp()

  @combinations.generate(maybe_distribute)
  def test_read(self, distribution):
    with distribution.scope():
      x = get_var(1., dtypes.float32)
      x = autocast_variable.create_autocast_variable(x)
      self.evaluate(x.initializer)

      # outside of auto cast scope.
      self.assertEqual(x.dtype, dtypes.float32)
      self.assertEqual(x.value().dtype, dtypes.float32)
      self.assertEqual(x.read_value().dtype, dtypes.float32)
      self.assertEqual(array_ops.identity(x).dtype, dtypes.float32)

      # within auto cast scope of different dtype
      with ops.get_default_graph()._enable_auto_casting_variables(
          dtypes.float16):
        self.assertEqual(x.dtype, dtypes.float16)
        self.assertEqual(x.value().dtype, dtypes.float16)
        self.assertEqual(x.read_value().dtype, dtypes.float16)
        self.assertEqual(array_ops.identity(x).dtype, dtypes.float16)

      # within auto cast scope of same dtype
      with ops.get_default_graph()._enable_auto_casting_variables(
          dtypes.float32):
        self.assertEqual(x.dtype, dtypes.float32)
        self.assertEqual(x.value().dtype, dtypes.float32)
        self.assertEqual(x.read_value().dtype, dtypes.float32)
        self.assertEqual(array_ops.identity(x).dtype, dtypes.float32)

  def test_sparse_reads(self):
    x = get_var([1., 2], dtypes.float32)
    # DistributedVariables do not support sparse_read or gather_nd, so we pass
    # distribute=False
    x = autocast_variable.create_autocast_variable(x)
    self.evaluate(x.initializer)

    self.assertEqual(x.sparse_read([0]).dtype, dtypes.float32)
    self.assertEqual(x.gather_nd([0]).dtype, dtypes.float32)

    with ops.get_default_graph()._enable_auto_casting_variables(
        dtypes.float16):
      self.assertEqual(x.sparse_read([0]).dtype, dtypes.float16)
      self.assertEqual(x.gather_nd([0]).dtype, dtypes.float16)

  @combinations.generate(maybe_distribute)
  def test_read_nested_scopes(self, distribution):
    with distribution.scope():
      x = get_var(1., dtypes.float32)
      x = autocast_variable.create_autocast_variable(x)
      self.evaluate(x.initializer)

      with ops.get_default_graph()._enable_auto_casting_variables(
          dtypes.float16):
        self.assertEqual(x.dtype, dtypes.float16)
        self.assertEqual(x.read_value().dtype, dtypes.float16)

        with ops.get_default_graph()._enable_auto_casting_variables(
            dtypes.float32):
          self.assertEqual(x.dtype, dtypes.float32)
          self.assertEqual(x.read_value().dtype, dtypes.float32)

        self.assertEqual(x.dtype, dtypes.float16)
        self.assertEqual(x.read_value().dtype, dtypes.float16)

  @combinations.generate(maybe_distribute)
  def test_dtype_is_not_string(self, distribution):
    with distribution.scope():
      x = get_var(1., dtypes.float32)
      x = autocast_variable.create_autocast_variable(x)
      self.assertEqual(x.dtype, dtypes.float32)
      self.assertIsInstance(x.dtype, dtypes.DType)
      self.assertEqual(x.true_dtype, dtypes.float32)
      self.assertIsInstance(x.true_dtype, dtypes.DType)

      dtype = dtypes.float16
      with ops.get_default_graph()._enable_auto_casting_variables(dtype):
        self.assertEqual(x.dtype, dtypes.float16)
        self.assertIsInstance(x.dtype, dtypes.DType)
        self.assertEqual(x.true_dtype, dtypes.float32)
        self.assertIsInstance(x.true_dtype, dtypes.DType)

  @combinations.generate(maybe_distribute)
  def test_method_delegations(self, distribution):
    # Test AutoCastVariable correctly delegates Variable methods to the
    # underlying variable.
    with self.test_session(), distribution.scope():
      for read_dtype in (dtypes.float32, dtypes.float16):
        if ds_context.has_strategy():
          # MirroredVariable.assign will (incorrectly) return a Mirrored value
          # instead of a MirroredVariable. So we cannot properly wrap it in an
          # AutoCastVariable.
          evaluate = self.evaluate
        else:

          def evaluate(var):
            self.assertIsInstance(var, autocast_variable.AutoCastVariable)
            self.assertEqual(var.dtype, read_dtype)
            return self.evaluate(var)

        x = get_var(7., dtypes.float32)
        x = autocast_variable.create_autocast_variable(x)
        with ops.get_default_graph()._enable_auto_casting_variables(
            read_dtype):
          self.evaluate(x.initializer)
          self.assertEqual(self.evaluate(x.value()), 7)
          self.assertEqual(self.evaluate(x.read_value()), 7)
          self.assertTrue(x.trainable)
          self.assertEqual(x.synchronization, x._variable.synchronization)
          self.assertEqual(x.aggregation, x._variable.aggregation)
          self.assertEqual(self.evaluate(x.initialized_value()), 7)
          if not context.executing_eagerly():
            if not ds_context.has_strategy():
              # These functions are not supported for DistributedVariables
              x.load(9)
              self.assertEqual(x.eval(), 9)
            self.assertEqual(self.evaluate(x.initial_value), 7)
            self.assertEqual(x.op, x._variable.op)
            self.assertEqual(x.graph, x._variable.graph)
          if not ds_context.has_strategy():
            # These attributes are not supported for DistributedVariables
            self.assertIsNone(x.constraint)
            self.assertEqual(x.initializer, x._variable.initializer)
          self.assertEqual(evaluate(x.assign(8)), 8)
          self.assertEqual(evaluate(x.assign_add(2)), 10)
          self.assertEqual(evaluate(x.assign_sub(3)), 7)
          self.assertEqual(x.name, x._variable.name)
          self.assertEqual(x.device, x._variable.device)
          self.assertEqual(x.shape, ())
          self.assertEqual(x.get_shape(), ())

        if not ds_context.has_strategy():
          # Test scatter_* methods. These are not supported for
          # DistributedVariables
          x = get_var([7, 8], dtypes.float32)
          x = autocast_variable.create_autocast_variable(x)
          with ops.get_default_graph()._enable_auto_casting_variables(
              read_dtype):
            self.evaluate(x.initializer)
            self.assertAllEqual(self.evaluate(x.value()), [7, 8])

            def slices(val, index):
              return indexed_slices.IndexedSlices(
                  values=constant_op.constant(val, dtype=dtypes.float32),
                  indices=constant_op.constant(index, dtype=dtypes.int32),
                  dense_shape=constant_op.constant([2], dtype=dtypes.int32))

            self.assertAllEqual(evaluate(x.scatter_sub(slices(1., 0))), [6, 8])
            self.assertAllEqual(evaluate(x.scatter_add(slices(1., 0))), [7, 8])
            self.assertAllEqual(evaluate(x.scatter_max(slices(9., 1))), [7, 9])
            self.assertAllEqual(evaluate(x.scatter_min(slices(8., 1))), [7, 8])
            self.assertAllEqual(evaluate(x.scatter_mul(slices(2., 1))), [7, 16])
            self.assertAllEqual(evaluate(x.scatter_div(slices(2., 1))), [7, 8])
            self.assertAllEqual(
                evaluate(x.scatter_update(slices(4., 1))), [7, 4])
            self.assertAllEqual(
                evaluate(x.scatter_nd_sub([[0], [1]], [1., 2.])), [6, 2])
            self.assertAllEqual(
                evaluate(x.scatter_nd_add([[0], [1]], [1., 2.])), [7, 4])
            self.assertAllEqual(
                evaluate(x.scatter_nd_update([[0], [1]], [1., 2.])), [1, 2])

  @combinations.generate(maybe_distribute)
  def test_operator_overloads(self, distribution):
    with distribution.scope():
      for read_dtype in (dtypes.float32, dtypes.float16):
        x = get_var(7., dtypes.float32)
        x = autocast_variable.create_autocast_variable(x)
        with ops.get_default_graph()._enable_auto_casting_variables(
            read_dtype):
          self.evaluate(x.initializer)
          self.assertAlmostEqual(8, self.evaluate(x + 1))
          self.assertAlmostEqual(10, self.evaluate(3 + x))
          self.assertAlmostEqual(14, self.evaluate(x + x))
          self.assertAlmostEqual(5, self.evaluate(x - 2))
          self.assertAlmostEqual(6, self.evaluate(13 - x))
          self.assertAlmostEqual(0, self.evaluate(x - x))
          self.assertAlmostEqual(14, self.evaluate(x * 2))
          self.assertAlmostEqual(21, self.evaluate(3 * x))
          self.assertAlmostEqual(49, self.evaluate(x * x))
          self.assertAlmostEqual(3.5, self.evaluate(x / 2))
          self.assertAlmostEqual(1.5, self.evaluate(10.5 / x))
          self.assertAlmostEqual(3, self.evaluate(x // 2))
          self.assertAlmostEqual(2, self.evaluate(15 // x))
          if read_dtype == dtypes.float32:
            # The "mod" operator does not support float16
            self.assertAlmostEqual(1, self.evaluate(x % 2))
            self.assertAlmostEqual(2, self.evaluate(16 % x))
          self.assertTrue(self.evaluate(x < 12))
          self.assertTrue(self.evaluate(x <= 12))
          self.assertFalse(self.evaluate(x > 12))
          self.assertFalse(self.evaluate(x >= 12))
          self.assertFalse(self.evaluate(12 < x))
          self.assertFalse(self.evaluate(12 <= x))
          self.assertTrue(self.evaluate(12 > x))
          self.assertTrue(self.evaluate(12 >= x))
          self.assertAlmostEqual(343, self.evaluate(pow(x, 3)), places=4)
          self.assertAlmostEqual(128, self.evaluate(pow(2, x)), places=4)
          self.assertAlmostEqual(-7, self.evaluate(-x))
          self.assertAlmostEqual(7, self.evaluate(abs(x)))

          x = get_var([7, 8, 9], dtypes.float32)
          x = autocast_variable.create_autocast_variable(x)
          self.evaluate(x.initializer)
          self.assertEqual(self.evaluate(x[1]), 8)
          if tf2.enabled() and context.executing_eagerly():
            self.assertAllEqual(x == [7., 8., 10.], [True, True, False])
            self.assertAllEqual(x != [7., 8., 10.], [False, False, True])

  @combinations.generate(maybe_distribute)
  def test_assign(self, distribution):
    with distribution.scope():
      x = get_var(0., dtypes.float32)
      x = autocast_variable.create_autocast_variable(x)
      self.evaluate(x.initializer)

      # outside of auto cast scope.
      v1 = constant_op.constant(3., dtype=dtypes.float32)
      v2 = constant_op.constant(3., dtype=dtypes.float16)

      def run_and_check():
        # Assign float32 values
        self.assertAllClose(3., self.evaluate(x.assign(v1)))
        self.assertAllClose(3. * 2, self.evaluate(x.assign_add(v1)))
        self.assertAllClose(3., self.evaluate(x.assign_sub(v1)))

        # Attempt to assign float16 values
        with self.assertRaisesRegexp(
            ValueError,
            'conversion requested dtype float32 for Tensor with dtype float16'):
          self.evaluate(x.assign(v2))
        with self.assertRaisesRegexp(
            ValueError,
            'conversion requested dtype float32 for Tensor with dtype float16'):
          self.evaluate(x.assign_add(v2))
        with self.assertRaisesRegexp(
            ValueError,
            'conversion requested dtype float32 for Tensor with dtype float16'):
          self.evaluate(x.assign_sub(v2))

        # Assign Python floats
        self.assertAllClose(0., self.evaluate(x.assign(0.)))
        self.assertAllClose(3., self.evaluate(x.assign(3.)))
        self.assertAllClose(3. * 2, self.evaluate(x.assign_add(3.)))
        self.assertAllClose(3., self.evaluate(x.assign_sub(3.)))

        # Assign multiple times
        # This currently only works if no strategy is used
        if not ds_context.has_strategy():
          assign = x.assign(1.)
          self.assertAllClose(1., self.evaluate(assign))
          self.assertAllClose(0., self.evaluate(assign.assign(0.)))
          assign_add = x.assign_add(3.)
          self.assertAllClose(3., self.evaluate(assign_add))
          self.assertAllClose(3. * 3,
                              self.evaluate(x.assign_add(3.).assign_add(3.)))
          self.assertAllClose(3. * 3, x)
          assign_sub = x.assign_sub(3.)
          self.assertAllClose(3. * 2, self.evaluate(assign_sub))
          self.assertAllClose(0.,
                              self.evaluate(x.assign_sub(3.).assign_sub(3.)))

        # Assign with read_value=False
        self.assertIsNone(self.evaluate(x.assign(1., read_value=False)))
        self.assertAllClose(1., self.evaluate(x))
        self.assertIsNone(self.evaluate(x.assign_add(2., read_value=False)))
        self.assertAllClose(3., self.evaluate(x))
        self.assertIsNone(self.evaluate(x.assign_sub(3., read_value=False)))
        self.assertAllClose(0., self.evaluate(x))

        # Use the tf.assign functions instead of the var.assign methods.
        self.assertAllClose(0., self.evaluate(state_ops.assign(x, 0.)))
        self.assertAllClose(3., self.evaluate(state_ops.assign(x, 3.)))
        self.assertAllClose(3. * 2,
                            self.evaluate(state_ops.assign_add(x, 3.)))
        self.assertAllClose(3., self.evaluate(state_ops.assign_sub(x, 3.)))

      run_and_check()
      # reset x
      self.evaluate(x.assign(0.))
      # within auto cast scope.
      with ops.get_default_graph()._enable_auto_casting_variables(
          dtypes.float16):
        # assign still expect float32 value even if in float16 scope
        run_and_check()

  @combinations.generate(maybe_distribute)
  def test_assign_stays_in_true_dtype(self, distribution):
    with distribution.scope():
      x = get_var(1., dtypes.float32)
      x = autocast_variable.create_autocast_variable(x)
      self.evaluate(x.initializer)
      # small_val is a value such that 1.0 + small_val == 1.0 in fp16, but not
      # in fp32
      small_val = np.finfo('float16').eps / 2
      small_tensor = constant_op.constant(small_val, dtype=dtypes.float32)
      with ops.get_default_graph()._enable_auto_casting_variables(
          dtypes.float16):
        # Variable should be increased, despite it appearing to be the same
        # float16 value.
        self.assertEqual(1. + small_val,
                         self.evaluate(x.assign(1. + small_tensor)))
        self.assertEqual(1., self.evaluate(x.value()))
      self.assertEqual(1. + small_val, self.evaluate(x.value()))

      self.evaluate(x.assign(1.))
      with ops.get_default_graph()._enable_auto_casting_variables(
          dtypes.float16):
        self.assertEqual(1. + small_val,
                         self.evaluate(x.assign_add(small_tensor)))
        self.assertEqual(1., self.evaluate(x.value()))
      self.assertEqual(1. + small_val, self.evaluate(x.value()))

  @combinations.generate(maybe_distribute)
  def test_checkpoint(self, distribution):
    with self.test_session():
      with distribution.scope():
        x = get_var(1., dtypes.float32)
        x = autocast_variable.create_autocast_variable(x)
      self.evaluate(x.initializer)
      self.evaluate(x.assign(123.))

      checkpoint = trackable_utils.Checkpoint(x=x)
      prefix = os.path.join(self.get_temp_dir(), 'ckpt')
      save_path = checkpoint.save(prefix)
      self.evaluate(x.assign(234.))
      checkpoint.restore(save_path).assert_consumed().run_restore_ops()
      self.assertEqual(self.evaluate(x), 123.)

  @combinations.generate(maybe_distribute)
  def test_invalid_wrapped_variable(self, distribution):
    with distribution.scope():
      # Wrap a non-variable
      with self.assertRaisesRegexp(ValueError, 'variable must be of type'):
        x = constant_op.constant([1.], dtype=dtypes.float32)
        autocast_variable.create_autocast_variable(x)

      # Wrap a non-floating point variable
      with self.assertRaisesRegexp(ValueError,
                                   'variable must be a floating point'):
        x = get_var(1, dtypes.int32)
        autocast_variable.create_autocast_variable(x)

  def test_repr(self):
    # We do not test with DistributionStrategy because we do not want to rely on
    # the exact __repr__ output of a DistributedVariable.
    x = get_var(1., dtypes.float32, name='x')
    x = autocast_variable.create_autocast_variable(x)
    if context.executing_eagerly():
      self.assertStartsWith(
          repr(x),
          "<AutoCastVariable 'x:0' shape=() dtype=float32 true_dtype=float32, "
          "numpy="
      )
      with ops.get_default_graph()._enable_auto_casting_variables(
          dtypes.float16):
        self.assertStartsWith(
            repr(x),
            "<AutoCastVariable 'x:0' shape=() dtype=float16 "
            "true_dtype=float32, numpy="
        )
    else:
      self.assertEqual(
          repr(x),
          "<AutoCastVariable 'x:0' shape=() dtype=float32 true_dtype=float32>"
      )
      with ops.get_default_graph()._enable_auto_casting_variables(
          dtypes.float16):
        self.assertEqual(
            repr(x),
            "<AutoCastVariable 'x:0' shape=() dtype=float16 true_dtype=float32>"
        )

  def test_repr_distributed(self):
    with mirrored_strategy.MirroredStrategy(["/cpu:1", "/cpu:2"]).scope():
      x = get_var(1., dtypes.float32)
      x = autocast_variable.create_autocast_variable(x)
      self.assertRegexpMatches(
          repr(x).replace('\n', ' '),
          '<AutoCastDistributedVariable dtype=float32 true_dtype=float32 '
          'inner_variable=MirroredVariable.*>'
      )

  @parameterized.named_parameters(
      ('v1', gradient_descent_v1.GradientDescentOptimizer),
      ('v2', gradient_descent_v2.SGD))
  def test_optimizer(self, optimizer_class):
    x = get_var(1., dtypes.float32)
    x = autocast_variable.create_autocast_variable(x)
    opt = optimizer_class(1.)

    @def_function.function
    def f():
      opt.minimize(lambda: x + 1., var_list=[x])

    if context.executing_eagerly():
      f()
    else:
      op = f()  # pylint: disable=assignment-from-no-return
      self.evaluate(variables.global_variables_initializer())
      self.evaluate(op)
    self.assertEqual(self.evaluate(x), 0)


if __name__ == '__main__':
  test.main()
